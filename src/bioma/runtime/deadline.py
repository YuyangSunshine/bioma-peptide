"""Monotonic execution budget. Late model results cannot authorize another action.

The bounded waiter does not claim to kill an already dispatched remote operation.
External effects still require reconciliation; benchmark workers use process isolation.
"""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from queue import Queue, Empty
from threading import Thread
import time


DEFAULT_EXECUTION_SECONDS = 300.0


class ExecutionTimeout(TimeoutError):
    pass


_limit = ContextVar('bioma_deadline', default=None)


def remaining():
    limit = _limit.get()
    return None if limit is None else max(0.0, limit - time.monotonic())


def check():
    left = remaining()
    if left is not None and left <= 0:
        raise ExecutionTimeout('Task monotonic deadline exceeded')


@contextmanager
def deadline(seconds):
    import math
    if not math.isfinite(seconds):
        raise ValueError('deadline must be finite')
    old = _limit.get()
    limit = time.monotonic() + seconds
    token = _limit.set(min(old, limit) if old is not None else limit)
    try:
        yield
    finally:
        _limit.reset(token)


def bounded_call(action):
    check()
    left = remaining()
    if left is None:
        return action()
    result = Queue(maxsize=1)
    context = copy_context()
    from bioma.runtime.effects import _context as effects_context
    effects = effects_context.get()
    ledger = effects[0].ledger if effects else None
    lease = getattr(ledger.lease_context, 'token', None) if ledger else None
    def run():
        if ledger:
            ledger.lease_context.token = lease
        try:
            result.put((True, context.run(action)))
        except BaseException as exc:
            result.put((False, exc))
        finally:
            if ledger:
                ledger.lease_context.token = None
    Thread(target=run, daemon=True).start()
    try:
        ok, value = result.get(timeout=left)
    except Empty:
        raise ExecutionTimeout('Task monotonic deadline exceeded while awaiting a call') from None
    check()
    if not ok:
        raise value
    return value
