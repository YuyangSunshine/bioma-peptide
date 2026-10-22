"""Per-node immutable facts, durable replication outbox and explicit conflicts."""
import hashlib
import json
import sqlite3
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class NodeJournal:
    def __init__(self, path, node_id):
        self.path, self.node_id = Path(path), node_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, fact_key TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS outbox(peer TEXT, id TEXT, delivered INTEGER DEFAULT 0, PRIMARY KEY(peer,id));
                CREATE TABLE IF NOT EXISTS conflicts(fact_key TEXT, first_id TEXT, second_id TEXT, PRIMARY KEY(fact_key,first_id,second_id));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def publish(self, key, kind, payload, parents=(), peers=()):
        body = {'key': key, 'kind': kind, 'payload': payload, 'parents': sorted(parents)}
        event = {'id': hashlib.sha256(canonical(body).encode()).hexdigest(), **body}
        self.receive(event, peers=peers)
        return event

    def receive(self, event, peers=()):
        body = {k: v for k, v in event.items() if k != 'id'}
        if hashlib.sha256(canonical(body).encode()).hexdigest() != event['id']:
            raise ValueError('Replicated event hash mismatch')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            others = db.execute('SELECT id FROM facts WHERE fact_key=? AND id<>?', (event['key'], event['id'])).fetchall()
            for (other,) in others:
                first, second = sorted((other, event['id']))
                db.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?,?)', (event['key'], first, second))
            db.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?)', (event['id'], event['key'], canonical(event)))
            for peer in peers:
                db.execute('INSERT OR IGNORE INTO outbox(peer,id) VALUES(?,?)', (peer, event['id']))

    def events(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM facts ORDER BY id')]

    def projection(self):
        with self.connect() as db:
            disputed = {r[0] for r in db.execute('SELECT fact_key FROM conflicts')}
        pending = {e['id']: e for e in self.events() if e['key'] not in disputed}
        accepted = {}
        while True:
            ready = [e for e in pending.values() if set(e['parents']) <= accepted.keys()]
            if not ready:
                break
            for e in ready:
                accepted[e['id']] = pending.pop(e['id'])
        return {'facts': sorted(accepted.values(), key=lambda e: e['id']),
                'pending': sorted(pending), 'conflicts': sorted(disputed)}

    def flush(self, peer, send):
        with self.connect() as db:
            rows = db.execute('SELECT f.id,f.body FROM facts f JOIN outbox o ON f.id=o.id WHERE o.peer=? AND o.delivered=0 ORDER BY f.id', (peer,)).fetchall()
        for event_id, body in rows:
            send(json.loads(body))  # ACK only after durable remote acceptance.
            with self.connect() as db:
                db.execute('UPDATE outbox SET delivered=1 WHERE peer=? AND id=?', (peer, event_id))
        return len(rows)
