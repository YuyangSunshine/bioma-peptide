"""Task ownership shared by processes using the same ledger database."""
import time
from contextlib import contextmanager, closing
from threading import Event, Thread
from uuid import uuid4


class TaskLeaseBusy(RuntimeError):
    pass


@contextmanager
def task_lease(ledger, run_id, seconds=30, *, lease_key=None):
    lease_key = lease_key or run_id
    if seconds <= 0:
        raise ValueError("Lease duration must be positive")
    owner = str(uuid4())
    with closing(ledger._connect()) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS task_leases(run_id TEXT PRIMARY KEY, owner TEXT, expires REAL)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT expires FROM task_leases WHERE run_id=?', (lease_key,)).fetchone()
        if row and row[0] > time.time():
            raise TaskLeaseBusy('Task lease is held by another runtime')
        db.execute('INSERT INTO task_leases VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires', (lease_key, owner, time.time()+seconds))
    stop = Event()
    def renew():
        while not stop.wait(seconds / 3):
            with closing(ledger._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                now = time.time()
                changed = db.execute('UPDATE task_leases SET expires=? WHERE run_id=? AND owner=? AND expires>?', (now+seconds, lease_key, owner, now))
                if not changed.rowcount:
                    return
    thread = Thread(target=renew, daemon=True)
    ledger.lease_context.token = (run_id, owner, lease_key)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        ledger.lease_context.token = None
        with closing(ledger._connect()) as db, db:
            db.execute('DELETE FROM task_leases WHERE run_id=? AND owner=?', (lease_key, owner))
