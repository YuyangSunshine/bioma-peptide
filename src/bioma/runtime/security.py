"""Pinned Ed25519 identities and deny-by-default application permissions.

Keys and grants are host configuration, never accepted from an agent payload.
TLS remains required for confidentiality outside loopback.
"""
from dataclasses import dataclass, field
import base64
import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Identity:
    key_id: str
    private_key: Ed25519PrivateKey = field(repr=False)

    @classmethod
    def generate(cls, key_id):
        return cls(key_id, Ed25519PrivateKey.generate())

    @classmethod
    def from_private_hex(cls, key_id, value):
        return cls(key_id, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value)))

    @property
    def public_hex(self):
        return self.private_key.public_key().public_bytes_raw().hex()

    def sign(self, purpose, payload):
        body = {'key_id': self.key_id, 'purpose': purpose, 'payload': payload}
        return {**body, 'signature': base64.b64encode(self.private_key.sign(canonical(body))).decode()}


@dataclass(frozen=True)
class Principal:
    public_key: str
    actions: frozenset[str] = frozenset()
    runs: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    agents: frozenset[str] = frozenset()


class TrustStore:
    def __init__(self, principals=None):
        self.principals = dict(principals or {})

    @classmethod
    def from_dict(cls, rows):
        return cls({key: Principal(public_key=row['public_key'], **{
            name: frozenset(row.get(name, [])) for name in ('actions', 'runs', 'resources', 'agents')})
            for key, row in rows.items()})

    def snapshot(self):
        return {key: {'public_key': p.public_key, **{
            name: sorted(getattr(p, name)) for name in ('actions', 'runs', 'resources', 'agents')}}
            for key, p in sorted(self.principals.items())}

    def require(self, principal, action, *, run_id=None, resource=None, agent=None):
        p = self.principals.get(principal)
        if p is None or action not in p.actions:
            raise PermissionError('Principal is not authorized for ' + action)
        for value, allowed in ((run_id, p.runs), (resource, p.resources), (agent, p.agents)):
            if value is not None and value not in allowed and '*' not in allowed:
                raise PermissionError('Principal scope does not cover the requested operation')

    def verify(self, envelope, purpose):
        try:
            key_id = envelope['key_id']
            if set(envelope) != {'key_id', 'purpose', 'payload', 'signature'} or envelope['purpose'] != purpose:
                raise ValueError('Invalid signature envelope')
            body = {k: envelope[k] for k in ('key_id', 'purpose', 'payload')}
            public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.principals[key_id].public_key))
            public.verify(base64.b64decode(envelope['signature'], validate=True), canonical(body))
        except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
            raise PermissionError('Unknown identity or invalid signature') from exc
        return key_id, envelope['payload']
