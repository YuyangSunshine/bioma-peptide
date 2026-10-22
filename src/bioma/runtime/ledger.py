from __future__ import annotations

import hashlib
from contextlib import closing
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, local
import time
from typing import Any, Iterable
from uuid import uuid4


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    event_id: str
    run_id: str
    kind: str
    actor: str
    payload: dict[str, Any]
    idempotency_key: str | None
    previous_hash: str
    event_hash: str
    created_at: str


class RunLedger:
    """SQLite append-only event log with a deterministic per-run hash chain."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.lease_context = local()
        if read_only:
            return
        with closing(self._connect()) as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS run_events(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
              kind TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL,
              idempotency_key TEXT, previous_hash TEXT NOT NULL,
              event_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_run_idempotency
              ON run_events(run_id,idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS run_event_order ON run_events(run_id,sequence);
            CREATE INDEX IF NOT EXISTS run_event_kind ON run_events(run_id,kind,sequence);
            CREATE TABLE IF NOT EXISTS conflicts(
              conflict_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, existing_hash TEXT NOT NULL,
              proposed_payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = (sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=30)
              if self.read_only else sqlite3.connect(self.path, timeout=30))
        db.row_factory = sqlite3.Row
        return db

    def append(self, run_id: str, kind: str, actor: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> LedgerEvent:
        body = _canonical(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            token = getattr(self.lease_context, 'token', None)
            if token:
                lease_key = token[2] if len(token) > 2 else run_id
                lease = db.execute('SELECT owner,expires FROM task_leases WHERE run_id=?', (lease_key,)).fetchone()
                if token[0] != run_id or not lease or lease['owner'] != token[1] or lease['expires'] <= time.time():
                    raise RuntimeError('Task lease lost; commit rejected')
            if idempotency_key:
                row = db.execute("SELECT * FROM run_events WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key)).fetchone()
                if row:
                    if row["payload"] != body or row["kind"] != kind or row["actor"] != actor:
                        db.execute("INSERT INTO conflicts VALUES(?,?,?,?,?,?)", (str(uuid4()), run_id, idempotency_key, row["event_hash"], body, now))
                        db.commit()
                        raise ValueError(f"Idempotency conflict for {idempotency_key!r}.")
                    db.commit()
                    return self._event(row)
            if kind == 'TransitionCommitted':
                latest = db.execute("SELECT payload FROM run_events WHERE run_id=? AND kind IN ('TransitionCommitted','RunInitialized') ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
                if latest and json.loads(latest[0])['after'] != payload['before']:
                    raise ValueError('Scientific state compare-and-swap conflict; reproposal required')
            prior = db.execute("SELECT event_hash FROM run_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
            previous = prior[0] if prior else "0" * 64
            event_id = str(uuid4())
            digest = hashlib.sha256(_canonical({"event_id": event_id, "run_id": run_id, "kind": kind, "actor": actor, "payload": payload, "idempotency_key": idempotency_key, "previous_hash": previous, "created_at": now}).encode()).hexdigest()
            cur = db.execute("INSERT INTO run_events(event_id,run_id,kind,actor,payload,idempotency_key,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (event_id, run_id, kind, actor, body, idempotency_key, previous, digest, now))
            row = db.execute("SELECT * FROM run_events WHERE sequence=?", (cur.lastrowid,)).fetchone()
            db.commit()
            return self._event(row)

    def events(self, run_id: str, *, after_sequence: int = 0,
               kinds: tuple[str, ...] | None = None, latest: bool = False) -> tuple[LedgerEvent, ...]:
        # Select at SQLite before decoding large state snapshots. This changes
        # reads only: chain verification and replay still inspect the full run.
        query = "SELECT * FROM run_events WHERE run_id=? AND sequence>?"
        values = [run_id, after_sequence]
        if kinds is not None:
            if not kinds:
                return ()
            query += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            values.extend(kinds)
        query += " ORDER BY sequence DESC LIMIT 1" if latest else " ORDER BY sequence"
        with closing(self._connect()) as db, db:
            return tuple(self._event(row) for row in db.execute(query, values))

    def commit_transition(self, state, approved, message, receipt, pending_messages=()):
        """Persist verification inputs and derived state in one immutable event."""
        if approved.agent_id != 'harness' and approved.transition is None:
            raise ValueError('New production commits require WP1 execution proof')
        decision = None
        if approved.transition:
            from bioma.core.transitions import CommitDecision, verify_transition_proof
            verify_transition_proof(state, approved, message)
            proof = approved.transition
            events = self.events(state.task_id, kinds=('ExecutionPermitted', 'ExecutionResultRecorded'))
            for kind, key in (('ExecutionPermitted', 'permit'), ('ExecutionResultRecorded', 'result')):
                if not any(e.kind == kind and e.payload == proof[key] for e in events):
                    raise ValueError('Commit lacks journaled admission evidence: ' + kind)
            if receipt.decision != 'approved':
                raise ValueError('Commit requires an approved independent verification receipt')
            decision = CommitDecision(proposal_id=proof['permit']['proposal_id'],
                from_version=state.version, to_version=state.version+1,
                receipt_id=receipt.receipt_hash).model_dump(mode='json')
        candidate = state.commit(approved)
        event = self.append(state.task_id, "TransitionCommitted", approved.agent_id, {
            "before": state.model_dump(mode="json"),
            "delta": approved.model_dump(mode="json"),
            "message": message.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"),
            "receipt_input_contract": "state-expected-agent-v2",
            "verification_policy": receipt.policy_context,
            "commit_decision": decision,
            "after": candidate.model_dump(mode="json"),
            "outbox": [item.model_dump(mode='json') for item in (*pending_messages, *approved.messages)],
        }, idempotency_key=f"transition:{message.id}")
        from bioma.core.models import ScientificState
        return ScientificState.model_validate(event.payload["after"])

    @staticmethod
    def read_historical(path, run_id):
        """Read-only archival verification, never a runnable ScientificState.

        Validate recorded hashes and receipt bindings against recorded policy;
        do not execute that policy or silently bless it under today's verifier.
        """
        from bioma.core.receipts import VerificationReceipt
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            events=[RunLedger._event(row) for row in db.execute('SELECT * FROM run_events WHERE run_id=? ORDER BY sequence',(run_id,))]
        if not events:
            raise ValueError('Historical run not found')
        previous='0'*64
        state=None
        for event in events:
            body={k:getattr(event,k) for k in ('event_id','run_id','kind','actor','payload','idempotency_key','previous_hash','created_at')}
            if event.previous_hash!=previous or hashlib.sha256(_canonical(body).encode()).hexdigest()!=event.event_hash:
                raise ValueError('Historical event hash chain failed')
            previous=event.event_hash
            if event.kind!='TransitionCommitted':
                continue
            p=event.payload
            if p.get('receipt_input_contract')!='state-expected-agent-v2':
                raise ValueError('Unknown historical receipt schema; raw events require separate audit')
            if state is not None and state!=p['before']:
                raise ValueError('Historical snapshot discontinuity')
            delta={**p['delta'],'status':'proposed'}
            receipt=VerificationReceipt.model_validate(p['receipt'])
            expected=VerificationReceipt.calculate(run_id=run_id,verifier_id=receipt.verifier_id,
                inputs={'state':p['before'],'expected_agent':delta['agent_id'],'delta':delta,'source_message':p['message']},
                policy=p['verification_policy'],decision=receipt.decision,reasons=receipt.reasons)
            if expected.model_dump()!=receipt.model_dump() or receipt.decision!='approved':
                raise ValueError('Historical receipt binding failed')
            state=p['after']
        return {'run_id':run_id,'execution_eligible':False,'verification_scope':'recorded hashes and receipts; no current scientific qualification',
            'events':[e.__dict__ for e in events],'recorded_state':state,'last_event_hash':previous}

    def replay_state(self, run_id):
        from bioma.core.models import ScientificState, StateDelta, AgentMessage
        from bioma.core.verification import VerificationEngine
        state = None
        for event in self.events(run_id):
            if event.kind != "TransitionCommitted":
                continue
            before = ScientificState.model_validate(event.payload["before"])
            if state is not None and state.model_dump(mode="json") != before.model_dump(mode="json"):
                raise ValueError("Transition replay discontinuity")
            delta = StateDelta.model_validate(event.payload["delta"])
            original = delta.model_copy(update={'status': 'proposed'})
            recorded = event.payload['receipt']
            if event.payload.get('receipt_input_contract') != 'state-expected-agent-v2':
                raise ValueError('Unsupported receipt input contract; retain this ledger for audit and start a new run')
            from bioma.runtime.security import TrustStore
            policy = event.payload.get('verification_policy') or {}
            result, calculated = VerificationEngine(TrustStore.from_dict(policy.get('observation_trust', {})), historical=True).verify_with_receipt(
                before, original, expected_agent=delta.agent_id,
                source_message=AgentMessage.model_validate(event.payload['message']),
                verifier_id=recorded['verifier_id'])
            if calculated.policy_hash != recorded['policy_hash']:
                raise ValueError('Unsupported verification policy; retain this ledger for audit and start a new run')
            if not result.ok or calculated.model_dump(mode='json') != recorded:
                raise ValueError('Receipt replay verification failed')
            derived = before.commit(delta)
            after = ScientificState.model_validate(event.payload["after"])
            # Provenance IDs/timestamps are commit metadata, not reducer outputs.
            if derived.model_dump(exclude={"provenance"}) != after.model_dump(exclude={"provenance"}):
                raise ValueError("Transition replay mismatch")
            state = after
        return state

    def conflicts(self, run_id: str) -> tuple[dict[str, Any], ...]:
        with closing(self._connect()) as db, db:
            return tuple(dict(row) for row in db.execute("SELECT * FROM conflicts WHERE run_id=? ORDER BY created_at", (run_id,)))

    def verify_chain(self, run_id: str) -> bool:
        # append() stores canonical JSON. Hash those bytes without decoding and
        # re-encoding every increasingly large scientific snapshot on every sync.
        # Still read and verify EVERY event; never trust a cached head/cursor.
        previous = "0" * 64
        with closing(self._connect()) as db:
            for row in db.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,)):
                if row["previous_hash"] != previous:
                    return False
                body = {key: row[key] for key in ("event_id", "run_id", "kind", "actor",
                    "idempotency_key", "previous_hash", "created_at")}
                body["payload"] = None
                encoded = _canonical(body).replace('"payload":null', '"payload":' + row["payload"], 1)
                expected = hashlib.sha256(encoded.encode()).hexdigest()
                if expected != row["event_hash"]:
                    # Historical JSON may have equivalent noncanonical whitespace
                    # or key order. Preserve the original semantic hash contract.
                    try:
                        body["payload"] = json.loads(row["payload"])
                    except (ValueError, TypeError):
                        return False
                    if hashlib.sha256(_canonical(body).encode()).hexdigest() != row["event_hash"]:
                        return False
                previous = row["event_hash"]
        return True

    @staticmethod
    def _event(row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(row["sequence"], row["event_id"], row["run_id"], row["kind"], row["actor"], json.loads(row["payload"]), row["idempotency_key"], row["previous_hash"], row["event_hash"], row["created_at"])


    @staticmethod
    def _validate_dispatch_policy(policy):
        for limits in [policy['pair'], *policy['arms'].values()]:
            if any(type(limits.get(k)) is not int or limits[k] < 0 for k in ('requests', 'tokens', 'nanocny', 'tools')):
                raise ValueError('Budget limits must be nonnegative integers')
        for k in ('input_nanocny_per_token', 'output_nanocny_per_token'):
            if type(policy.get(k)) is not int or policy[k] < 0:
                raise ValueError('Rates must be integer nano-CNY per token')
        if policy.get('metering_mode') == 'usage_only_authorized' and not policy.get('authorization'):
            raise ValueError('Usage-only metering requires explicit authorization record')

    def configure_budget(self, pair: str, policy: dict) -> None:
        """Persist an immutable dispatch policy in the trusted existing ledger."""
        self._validate_dispatch_policy(policy)
        encoded = _canonical(policy)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS dispatch_policies(pair TEXT PRIMARY KEY, policy TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS dispatch_reservations(id TEXT PRIMARY KEY, pair TEXT NOT NULL, arm TEXT NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL, nanocny INTEGER NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL)')
            old = db.execute('SELECT policy FROM dispatch_policies WHERE pair=?', (pair,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError('Budget policy cannot change on resume')
            db.execute('INSERT OR IGNORE INTO dispatch_policies VALUES(?,?)', (pair, encoded))

    def amend_dispatch_limits(self, pair, *, expected_policy, policy, authorization):
        """Trusted operator-only explicit amendment; never reset dispatch history.

        Compare-and-swap prevents stale/concurrent amendments. Scope, rates and
        metering remain immutable; the supplied authorization is retained verbatim.
        This is not exposed to model tools or invoked by configure_budget.
        """
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError('Explicit authorization record required')
        self._validate_dispatch_policy(policy)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT policy FROM dispatch_policies WHERE pair=?', (pair,)).fetchone()
            if row is None or row[0] != _canonical(expected_policy):
                raise ValueError('Stale or missing budget policy')
            old = json.loads(row[0])
            scope = lambda p: {k:v for k,v in p.items() if k not in {'pair','arms'}}
            if scope(old) != scope(policy) or set(old['arms']) != set(policy['arms']):
                raise ValueError('Amendment cannot change endpoint, model, rates or metering scope')
            for arm, limits in [(None, policy['pair']), *policy['arms'].items()]:
                previous = old['pair'] if arm is None else old['arms'][arm]
                if set(limits) != set(previous):
                    raise ValueError('Amendment cannot change limit dimensions')
                where, params = ('pair=?', (pair,)) if arm is None else ('pair=? AND arm=?', (pair,arm))
                used = db.execute("SELECT COALESCE(SUM(kind='model'),0),COALESCE(SUM(tokens),0),COALESCE(SUM(nanocny),0),COALESCE(SUM(kind='tool'),0) FROM dispatch_reservations WHERE " + where, params).fetchone()
                for key, value in zip(('requests','tokens','nanocny','tools'), used):
                    if key in {'tokens','nanocny'} and policy.get('metering_mode') == 'usage_only_authorized':
                        if limits[key] != previous[key]:
                            raise ValueError('Usage-only accounting limits must remain unchanged')
                    elif limits[key] < value:
                        raise ValueError('Amended limit is below existing consumption')
            db.execute('CREATE TABLE IF NOT EXISTS dispatch_policy_amendments(id TEXT PRIMARY KEY,pair TEXT NOT NULL,before_policy TEXT NOT NULL,after_policy TEXT NOT NULL,authorization TEXT NOT NULL,created_at TEXT NOT NULL)')
            identity = str(uuid4())
            db.execute('INSERT INTO dispatch_policy_amendments VALUES(?,?,?,?,?,?)',
                (identity,pair,row[0],_canonical(policy),authorization,datetime.now(timezone.utc).isoformat()))
            db.execute('UPDATE dispatch_policies SET policy=? WHERE pair=?', (_canonical(policy),pair))
            return identity

    def reserve_dispatch(self, pair: str, arm: str, kind: str, tokens: int, nanocny: int, detail: dict) -> str:
        from bioma.llm import ModelBudgetUnavailable
        if kind not in {'model', 'tool'} or type(tokens) is not int or type(nanocny) is not int or min(tokens, nanocny) < 0:
            raise ValueError('Invalid reservation')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT policy FROM dispatch_policies WHERE pair=?', (pair,)).fetchone()
            if row is None:
                raise ModelBudgetUnavailable('No frozen budget policy')
            policy = json.loads(row[0])
            if arm not in policy['arms']:
                raise ModelBudgetUnavailable('Unrecognized budget arm')
            if db.execute("SELECT 1 FROM dispatch_reservations WHERE pair=? AND state='fault'", (pair,)).fetchone():
                raise ModelBudgetUnavailable('Usage exceeded trusted reservation; pair halted')
            for key, where, args in [('pair', 'pair=?', (pair,)), (arm, 'pair=? AND arm=?', (pair, arm))]:
                usage = db.execute("SELECT COALESCE(SUM(kind='model'),0),COALESCE(SUM(tokens),0),COALESCE(SUM(nanocny),0),COALESCE(SUM(kind='tool'),0) FROM dispatch_reservations WHERE " + where, args).fetchone()
                limits = policy['pair'] if key == 'pair' else policy['arms'][key]
                addition = (int(kind == 'model'), tokens, nanocny, int(kind == 'tool'))
                for used, added, field in zip(usage, addition, ('requests', 'tokens', 'nanocny', 'tools')):
                    if policy.get('metering_mode') == 'usage_only_authorized' and field in {'tokens', 'nanocny'}:
                        continue
                    if used + added > limits[field]:
                        raise ModelBudgetUnavailable(f'{key} {field} ceiling reached; not dispatched')
            identity = str(uuid4())
            db.execute('INSERT INTO dispatch_reservations VALUES(?,?,?,?,?,?,?,?)', (identity, pair, arm, kind, tokens, nanocny, 'reserved', _canonical(detail)))
            return identity

    def remaining_dispatches(self, pair: str, arm: str) -> dict[str, int]:
        """Advisory planning view; reserve_dispatch remains the atomic authority."""
        with closing(self._connect()) as db:
            policy = json.loads(db.execute('SELECT policy FROM dispatch_policies WHERE pair=?', (pair,)).fetchone()[0])
            remaining = {}
            for field, kind in [('requests', 'model'), ('tools', 'tool')]:
                total = db.execute('SELECT COUNT(*) FROM dispatch_reservations WHERE pair=? AND kind=?', (pair, kind)).fetchone()[0]
                used = db.execute('SELECT COUNT(*) FROM dispatch_reservations WHERE pair=? AND arm=? AND kind=?', (pair, arm, kind)).fetchone()[0]
                remaining[field] = max(0, min(policy['pair'][field]-total, policy['arms'][arm][field]-used))
            return remaining

    def settle_dispatch(self, identity: str, tokens: int, nanocny: int) -> None:
        from bioma.llm import ModelBudgetUnavailable
        if type(tokens) is not int or type(nanocny) is not int or min(tokens, nanocny) < 0:
            raise ValueError('Untrusted usage; retain reservation')
        fault = False
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT tokens,nanocny,state,pair FROM dispatch_reservations WHERE id=?', (identity,)).fetchone()
            if not row or row['state'] != 'reserved':
                raise ValueError('Reservation missing or already settled')
            policy = json.loads(db.execute('SELECT policy FROM dispatch_policies WHERE pair=?', (row['pair'],)).fetchone()[0])
            fault = policy.get('metering_mode') != 'usage_only_authorized' and (tokens > row['tokens'] or nanocny > row['nanocny'])
            db.execute('UPDATE dispatch_reservations SET tokens=?,nanocny=?,state=? WHERE id=?', (max(tokens,row['tokens']) if fault else tokens, max(nanocny,row['nanocny']) if fault else nanocny, 'fault' if fault else 'settled', identity))
        if fault:
            raise ModelBudgetUnavailable('Reported usage exceeds reserved bound; pair halted')


def replay(events: Iterable[LedgerEvent]) -> dict[str, Any]:
    """Build a deterministic symbolic graph projection from ledger events."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    for event in events:
        nodes[event.event_id] = {"id": event.event_id, "kind": event.kind, "actor": event.actor, "payload": event.payload, "sequence": event.sequence}
        if event.previous_hash != "0" * 64:
            edges.append({"source_hash": event.previous_hash, "target": event.event_id, "relation": "precedes"})
        for parent in event.payload.get("parent_ids", []):
            edges.append({"source": str(parent), "target": event.event_id, "relation": "supports"})
    return {"schema": "bioma-symbolic-projection-v1", "nodes": list(nodes.values()), "edges": edges}
