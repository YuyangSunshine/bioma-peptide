from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
import logging

from bioma.core.models import utc_now

EventSink = Callable[[dict[str, Any]], None]
_sink: ContextVar[EventSink | None] = ContextVar("bioma_event_sink", default=None)
_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "bioma_event_context", default=None
)


@contextmanager
def observe(sink: EventSink | None, **context: Any) -> Iterator[None]:
    sink_token = _sink.set(sink)
    context_token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(context_token)
        _sink.reset(sink_token)


def emit(event_type: str, **details: Any) -> None:
    from bioma.runtime.deadline import remaining
    left = remaining()
    if left is not None and left <= 0:
        return  # Ignore late worker progress after its authorization expired.
    sink = _sink.get()
    if sink is not None:
        sink(
            {
                "timestamp": utc_now().isoformat(),
                "type": event_type,
                **(_context.get() or {}),
                **details,
            }
        )


def notify_observer(sink: EventSink | None, event: dict[str, Any]) -> None:
    """Notify external observers without turning display errors into execution failures.

    Internal ledger writes remain outside this boundary and must propagate errors.
    """
    if sink is not None:
        try:
            sink(event)
        except Exception:
            logging.getLogger(__name__).exception('Runtime observer failed for %s', event.get('type'))
