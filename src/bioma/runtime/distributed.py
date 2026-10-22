from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ledger import RunLedger


class SQLiteTransport:
    """Persistent at-least-once queue with leases, heartbeats and reassignment."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS messages(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, topic TEXT NOT NULL,
              payload TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'queued', owner TEXT,
              lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
              available_at TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(run_id,idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS checkpoints(
              run_id TEXT NOT NULL, step_key TEXT NOT NULL, payload TEXT NOT NULL,
              version INTEGER NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(run_id,step_key)
            );
            CREATE TABLE IF NOT EXISTS delivery_failures(
              id TEXT PRIMARY KEY, message_id TEXT NOT NULL, owner TEXT NOT NULL,
              generation INTEGER NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL
            );
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def publish(self, run_id: str, topic: str, payload: dict[str, Any], idempotency_key: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        message_id = str(uuid4())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id,topic,payload FROM messages WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key)).fetchone()
            if prior is not None:
                if prior['topic'] != topic or json.loads(prior['payload']) != payload:
                    raise ValueError("Message idempotency conflict")
                return str(prior['id'])
            db.execute("INSERT OR IGNORE INTO messages(id,run_id,topic,payload,idempotency_key,available_at,created_at) VALUES(?,?,?,?,?,?,?)", (message_id, run_id, topic, json.dumps(payload, sort_keys=True), idempotency_key, now, now))
            row = db.execute("SELECT id FROM messages WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key)).fetchone()
        return str(row[0])

    def claim(self, node_id: str, topics: set[str], *, lease_seconds: int = 30) -> dict[str, Any] | None:
        if not topics:
            return None
        placeholders = ",".join("?" for _ in topics)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            db.execute("UPDATE messages SET state='queued',owner=NULL,lease_until=NULL WHERE state='leased' AND lease_until<=?", (now.isoformat(),))
            row = db.execute(f"SELECT * FROM messages WHERE state='queued' AND available_at<=? AND topic IN ({placeholders}) ORDER BY created_at,id LIMIT 1", (now.isoformat(), *sorted(topics))).fetchone()
            if not row:
                db.commit(); return None
            lease = (now + timedelta(seconds=lease_seconds)).isoformat()
            db.execute("UPDATE messages SET state='leased',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?", (node_id, lease, row["id"]))
            db.commit()
            result = dict(row); result.update(owner=node_id, lease_until=lease, state="leased", payload=json.loads(row["payload"]), attempts=row["attempts"] + 1)
            return result

    def heartbeat(self, message_id: str, node_id: str, *, lease_seconds: int = 30, generation: int = 1) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            lease = (now + timedelta(seconds=lease_seconds)).isoformat()
            cur = db.execute("UPDATE messages SET lease_until=? WHERE id=? AND owner=? AND attempts=? AND state='leased' AND lease_until>?", (lease, message_id, node_id, generation, now.isoformat()))
            return cur.rowcount == 1

    def completed_by(self, message_id, node_id, generation):
        """A successful atomic completion may race with the last heartbeat."""
        with self._db() as db:
            row = db.execute("SELECT c.payload FROM messages m JOIN checkpoints c "
                "ON c.run_id=m.run_id AND c.step_key=m.idempotency_key "
                "WHERE m.id=? AND m.owner=? AND m.attempts=? AND m.state='done'",
                (message_id,node_id,generation)).fetchone()
        return row is not None and 'result' in json.loads(row[0])

    def acknowledge(self, message_id: str, node_id: str, *, generation: int = 1) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE messages SET state='done',lease_until=NULL WHERE id=? AND owner=? AND attempts=? AND state='leased' AND lease_until>?", (message_id, node_id, generation, datetime.now(timezone.utc).isoformat()))
        return cur.rowcount == 1

    def fail(self, message_id: str, node_id: str, reason: str, *, retry_delay_seconds: int = 0, generation: int = 1) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc)
            available = (now + timedelta(seconds=retry_delay_seconds)).isoformat()
            cur = db.execute("UPDATE messages SET state='queued',owner=NULL,lease_until=NULL,available_at=? WHERE id=? AND owner=? AND attempts=? AND state='leased' AND lease_until>?", (available, message_id, node_id, generation, now.isoformat()))
            if cur.rowcount:
                db.execute('INSERT INTO delivery_failures VALUES(?,?,?,?,?,?)', (str(uuid4()), message_id, node_id, generation, reason, now.isoformat()))
        return cur.rowcount == 1

    def checkpoint(self, run_id: str, step_key: str, payload: dict[str, Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            row = db.execute("SELECT version FROM checkpoints WHERE run_id=? AND step_key=?", (run_id, step_key)).fetchone()
            version = (row[0] if row else 0) + 1
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(run_id,step_key) DO UPDATE SET payload=excluded.payload,version=excluded.version,updated_at=excluded.updated_at", (run_id, step_key, json.dumps(payload, sort_keys=True), version, now))
        return version

    def load_checkpoint(self, run_id: str, step_key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT payload,version,updated_at FROM checkpoints WHERE run_id=? AND step_key=?", (run_id, step_key)).fetchone()
        return None if row is None else {"payload": json.loads(row[0]), "version": row[1], "updated_at": row[2]}

    def complete(self, message, result):
        """Fence by claim generation and atomically checkpoint plus acknowledge."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = datetime.now(timezone.utc).isoformat()
            cur = db.execute("UPDATE messages SET state='done',lease_until=NULL WHERE id=? AND owner=? AND attempts=? AND state='leased' AND lease_until>?",
                (message['id'], message['owner'], message['attempts'], now))
            if cur.rowcount != 1:
                raise ValueError('Lease lost before checkpoint commit')
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(run_id,step_key) DO UPDATE SET payload=excluded.payload,version=checkpoints.version+1,updated_at=excluded.updated_at",
                (message['run_id'], message['idempotency_key'], json.dumps({'result': result}, sort_keys=True), 1, now))


class HarnessNode:
    def __init__(self, node_id: str, transport: SQLiteTransport, topics: set[str], handler, *, raise_errors=False) -> None:
        self.node_id, self.transport, self.topics, self.handler = node_id, transport, topics, handler
        self.raise_errors = raise_errors

    def step(self) -> bool:
        message = self.transport.claim(self.node_id, self.topics)
        if message is None:
            return False
        try:
            checkpoint = self.transport.load_checkpoint(message["run_id"], message["idempotency_key"])
            completed = (
                checkpoint is not None
                and isinstance(checkpoint["payload"], dict)
                and "result" in checkpoint["payload"]
            )
            if completed:
                # Older nodes could persist the result before ACK. Reuse it,
                # but only the current, unexpired delivery generation may ACK.
                if not self.transport.acknowledge(message["id"], self.node_id, generation=message["attempts"]):
                    raise ValueError("Lease lost before checkpoint acknowledgement")
            else:
                result = self.handler(message["payload"])
                self.transport.complete(message, result)
        except Exception as exc:
            self.transport.fail(message["id"], self.node_id, str(exc), retry_delay_seconds=1, generation=message['attempts'])
            if self.raise_errors:
                raise
        return True


class DeliverableExecutor:
    """Run dependency-free DAG levels concurrently and commit results by stable key."""

    def __init__(self, ledger: RunLedger, *, max_workers: int = 4) -> None:
        self.ledger, self.max_workers = ledger, max_workers

    def execute(self, run_id: str, deliverables: list[dict[str, Any]], actions: dict[str, Any]) -> dict[str, Any]:
        pending = {str(item["id"]): item for item in deliverables}
        completed: dict[str, Any] = {}
        for event in self.ledger.events(run_id):
            if event.kind == 'DeliverableVerified':
                item_id = str(event.payload['deliverable_id'])
                if item_id in pending:
                    if event.payload.get('contract') != pending[item_id]:
                        raise ValueError('Deliverable contract changed for existing result')
                    completed[item_id] = event.payload['value']
                    pending.pop(item_id)
        while pending:
            ready = [item for item in pending.values() if set(item.get("depends_on", ())) <= completed.keys()]
            if not ready:
                raise ValueError("Deliverable DAG contains a cycle or missing dependency.")
            # Reserve before invoking actions. An unresolved reservation is not
            # automatically retried: its external side effect may have happened.
            for item in ready:
                key = str(item.get('idempotency_key') or f"deliverable:{item['id']}")
                self.ledger.append(run_id, 'ActionReserved', str(item.get('provider') or 'worker'),
                    {'contract': item, 'execution_token': str(uuid4())},
                    idempotency_key='reservation:' + key)
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(actions[item["action"]], item): item for item in ready}
                level = []
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        value = future.result()
                        self.ledger.append(run_id, "DeliverableVerified", str(item.get("provider") or "worker"), {"deliverable_id": item["id"], "contract": item, "value": value, "parent_ids": item.get("depends_on", [])}, idempotency_key=str(item.get("idempotency_key") or f"deliverable:{item['id']}"))
                        level.append((str(item["id"]), value))
                    except Exception as exc:
                        self.ledger.append(run_id, "FailureEvidence", str(item.get("provider") or "worker"), {"deliverable_id": item["id"], "error_type": type(exc).__name__, "reason": str(exc), "compensation": item.get("compensation")}, idempotency_key=f"failure:{item['id']}")
                        raise
            for item_id, value in sorted(level):
                completed[item_id] = value
                pending.pop(item_id)
        return completed
