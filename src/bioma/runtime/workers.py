from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import json
import sqlite3
import hashlib
import inspect
import time
from pathlib import Path
from dataclasses import replace

from bioma.core.models import CapabilitySpec


class ProviderUnavailable(ValueError):
    def __init__(self, reason, *, execution_started=False):
        super().__init__(reason)
        self.execution_started = execution_started


@dataclass(frozen=True)
class CapabilityProvider:
    provider_id: str
    provider_kind: str
    capability: CapabilitySpec
    executor: Callable[[dict[str, Any]], dict[str, Any]]
    evaluation_score: float = 0.0
    release_stage: str = "shadow"


class VersionedProviderPool:
    """Competition protocol shared by persistent agents and ephemeral code workers."""

    STAGES = ("shadow", "canary", "stable")

    def __init__(self, path=None, *, trust=None) -> None:
        self._providers: dict[str, CapabilityProvider] = {}
        self.trust = trust
        self._evaluations = {}
        self._manifests = {}
        self._lifecycle = {}
        self.path = Path(path) if path is not None else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.execute('CREATE TABLE IF NOT EXISTS evaluations(key TEXT PRIMARY KEY, score REAL, stage TEXT, cases INTEGER, report TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS provider_manifests(key TEXT PRIMARY KEY, digest TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS provider_lifecycle(key TEXT PRIMARY KEY, status TEXT NOT NULL, generation INTEGER NOT NULL, expires REAL)')

    def lifecycle(self, key):
        if self.path:
            with sqlite3.connect(self.path) as db:
                row = db.execute('SELECT status,generation,expires FROM provider_lifecycle WHERE key=?', (key,)).fetchone()
            if row:
                return dict(status=row[0], generation=row[1], expires=row[2])
        return dict(self._lifecycle.get(key, dict(status='active', generation=1, expires=None)))

    def set_lifecycle(self, provider_id, capability_id, provider_kind, status, *,
                      implementation_version=None, lease_seconds=None, principal=None):
        """Drain prevents new offers; offline/revoked fence admitted execution too."""
        if status not in {'active', 'draining', 'offline', 'revoked'}:
            raise ValueError('Invalid capability lifecycle status')
        if lease_seconds is not None and lease_seconds <= 0:
            raise ValueError('Lease duration must be positive')
        key = self._resolve(provider_id, capability_id, provider_kind, implementation_version)
        if self.trust:
            self.trust.require(principal, 'provider.lifecycle', resource=key)
        def change(old):
            if old['status'] == 'revoked' and status != 'revoked':
                raise ValueError('Revoked versions cannot be reactivated; register a new version')
            # Draining retains existing permits. Other state changes fence them.
            generation = old['generation'] + int(status != old['status'] and status != 'draining')
            return dict(status=status, generation=generation,
                        expires=time.time()+lease_seconds if lease_seconds is not None else old['expires'])
        if self.path:
            with sqlite3.connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status,generation,expires FROM provider_lifecycle WHERE key=?',(key,)).fetchone()
                old = dict(status=row[0], generation=row[1], expires=row[2]) if row else dict(status='active',generation=1,expires=None)
                updated = change(old)
                db.execute('INSERT OR REPLACE INTO provider_lifecycle VALUES(?,?,?,?)',
                           (key, updated['status'], updated['generation'], updated['expires']))
        else:
            updated = change(self.lifecycle(key))
        self._lifecycle[key] = updated
        return updated

    def check_assignment(self, message):
        binding = message.payload.get('provider_binding')
        if not binding:
            return  # Explicit local direct assignments have no negotiated offer.
        try:
            key = self._resolve(message.recipient, message.payload['selected_capability'],
                                binding['provider_kind'], binding['implementation_version'])
        except ValueError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        self._check(key)
        current = self.lifecycle(key)
        if (current['status'] not in {'active', 'draining'} or
            current['generation'] != binding['generation'] or
            current['expires'] is not None and current['expires'] <= time.time()):
            raise ProviderUnavailable('Provider lease/version unavailable; execution must be renegotiated')

    def _available(self, key):
        state = self.lifecycle(key)
        return state['status'] == 'active' and (state['expires'] is None or state['expires'] > time.time())

    @staticmethod
    def _manifest(provider):
        try:
            source = inspect.getsource(provider.executor)
        except (OSError, TypeError):
            source = type(provider.executor).__module__ + ':' + type(provider.executor).__qualname__
        return hashlib.sha256(json.dumps({'capability': provider.capability.model_dump(mode='json'),
            'executor_source': source}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _check(self, key):
        if self._manifest(self._providers[key]) != self._manifests[key]:
            raise ValueError('Provider contract changed after registration')

    @staticmethod
    def _key(provider):
        version = provider.capability.runtime_profile.get('implementation_version', '1')
        return json.dumps([provider.provider_kind, provider.provider_id, provider.capability.id, str(version)], separators=(',', ':'))

    def _resolve(self, provider_id, capability_id, provider_kind, implementation_version=None):
        matches = [k for k, p in self._providers.items() if p.provider_id == provider_id
            and p.capability.id == capability_id and p.provider_kind == provider_kind
            and (implementation_version is None or str(p.capability.runtime_profile.get('implementation_version', '1')) == str(implementation_version))]
        if len(matches) != 1:
            raise ValueError('Provider version is missing or ambiguous; specify implementation_version')
        return matches[0]

    def register(self, provider: CapabilityProvider, *, principal=None) -> None:
        if self.trust is not None:
            import re
            if not re.fullmatch('[0-9a-f]{64}', str(provider.capability.runtime_profile.get('implementation_sha256', ''))):
                raise ValueError('Governed providers require an immutable implementation_sha256 manifest')
            self.trust.require(principal, 'provider.register', resource=self._key(provider))
            if provider.release_stage != 'shadow':
                self.trust.require(principal, 'provider.promote', resource=self._key(provider))
        if provider.release_stage not in self.STAGES:
            raise ValueError("Unknown release stage.")
        key = self._key(provider)
        manifest = self._manifest(provider)
        if key in self._providers:
            old = self._providers[key]
            if old.capability != provider.capability or old.executor is not provider.executor:
                raise ValueError('Immutable provider version already registered; increment implementation_version')
            return
        provider = replace(provider, capability=provider.capability.model_copy(deep=True))
        if self.path:
            with sqlite3.connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                saved = db.execute('SELECT digest FROM provider_manifests WHERE key=?', (key,)).fetchone()
                if saved and saved[0] != manifest:
                    raise ValueError('Immutable provider version changed across restart')
                db.execute('INSERT OR IGNORE INTO provider_manifests VALUES(?,?)', (key, manifest))
                row = db.execute('SELECT score,stage,cases FROM evaluations WHERE key=?', (key,)).fetchone()
            if row:
                provider = replace(provider, evaluation_score=row[0], release_stage=row[1])
                self._evaluations[key] = row[2]
        self._providers[key] = provider
        self._manifests[key] = manifest

    def evaluate(self, provider_id, capability_id, provider_kind, cases, validator, *, implementation_version=None, principal=None):
        """Execute held-out cases; callers cannot substitute a claimed score."""
        key = self._resolve(provider_id, capability_id, provider_kind, implementation_version)
        self._check(key)
        if self.trust is not None:
            self.trust.require(principal, 'provider.evaluate', resource=key)
        provider = self._providers[key]
        report = []
        for case in cases:
            try:
                result = provider.executor(case)
                passed = validator(case, result) is True
                report.append({'passed': passed})
            except Exception as exc:
                report.append({'passed': False, 'error_type': type(exc).__name__})
        if len(report) < 2:
            raise ValueError('At least two held-out evaluation cases are required')
        score = sum(x['passed'] for x in report) / len(report)
        updated = replace(provider, evaluation_score=score,
            release_stage=provider.release_stage if score >= 0.9 else 'shadow')
        self._providers[key] = updated
        self._evaluations[key] = len(report)
        if self.path:
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',
                    (key, score, updated.release_stage, len(report), json.dumps(report)))
        return updated

    def compete(self, capability_id: str, *, allowed_stages: set[str] | None = None) -> tuple[CapabilityProvider, ...]:
        for key in self._providers:
            self._check(key)
        allowed = allowed_stages or {"stable"}
        matches = [p for key,p in self._providers.items() if self._available(key) and p.capability.id == capability_id and p.release_stage in allowed and p.capability.enabled]
        return tuple(sorted(matches, key=lambda p: (-p.evaluation_score, -p.capability.reliability, p.provider_id)))

    def promote(self, provider_id: str, capability_id: str, provider_kind: str, *, minimum_score: float, implementation_version=None, principal=None) -> CapabilityProvider:
        key = self._resolve(provider_id, capability_id, provider_kind, implementation_version)
        self._check(key)
        if self.trust is not None:
            self.trust.require(principal, 'provider.promote', resource=key)
        if not 0 <= minimum_score <= 1 or self._evaluations.get(key, 0) < 2:
            raise ValueError('Promotion requires recorded evaluation cases and a valid threshold')
        current = self._providers[key]
        if current.evaluation_score < minimum_score:
            raise ValueError("Provider has not met the promotion threshold.")
        if self.path:
            with sqlite3.connect(self.path) as db:
                row = db.execute('SELECT cases FROM evaluations WHERE key=?', (key,)).fetchone()
            if not row or row[0] < 2:
                raise ValueError('Promotion requires recorded evaluation cases')
        index = min(self.STAGES.index(current.release_stage) + 1, len(self.STAGES) - 1)
        promoted = CapabilityProvider(current.provider_id, current.provider_kind, current.capability, current.executor, current.evaluation_score, self.STAGES[index])
        self._providers[key] = promoted
        if self.path:
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE evaluations SET stage=? WHERE key=?', (promoted.release_stage, key))
        return promoted

    def capabilities(self):
        for key in self._providers:
            self._check(key)
        groups = {}
        for p in self._providers.values():
            groups.setdefault((p.capability.agent_id, p.capability.id), []).append(p)
        selected = []
        for group in groups.values():
            stable = [p for p in group if p.release_stage == 'stable']
            if len(stable) > 1:
                raise ValueError('Multiple stable versions need distinct runtime peer identities')
            selected.append(stable[0] if stable else sorted(group, key=self._key)[0])
        return tuple(p.capability.model_copy(update={
            'enabled': p.capability.enabled and p.release_stage == 'stable' and self._available(self._key(p)),
            'reliability': p.evaluation_score if p.evaluation_score else p.capability.reliability,
            'runtime_profile': {**p.capability.runtime_profile, 'release_stage': p.release_stage,
                'provider_kind': p.provider_kind, 'evaluation_score': p.evaluation_score,
                'lifecycle': self.lifecycle(self._key(p))}})
            for p in selected)
