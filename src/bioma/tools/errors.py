from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Literal

ToolErrorType = Literal[
    "rate_limit",
    "timeout",
    "connection",
    "forbidden",
    "not_found",
    "unavailable",
    "invalid_input",
    "internal",
]


class ToolError(RuntimeError):
    """A tool failure with machine-readable immediate replay semantics."""

    def __init__(
        self,
        message: str,
        *,
        error_type: ToolErrorType,
        retriable: bool,
        backend: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retriable = retriable
        self.backend = backend
        self.retry_after_seconds = retry_after_seconds


def requests_tool_error(exc: Exception, *, backend: str) -> ToolError:
    """Convert a requests failure without leaking request URLs or credentials."""
    import requests

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return ToolError(
            f"{backend} rate limit exceeded.",
            error_type="rate_limit",
            retriable=True,
            backend=backend,
            retry_after_seconds=_retry_after_seconds(response),
        )
    if status_code in {401, 403}:
        return ToolError(
            f"{backend} rejected the request with HTTP {status_code}.",
            error_type="forbidden",
            retriable=False,
            backend=backend,
        )
    if status_code == 404:
        return ToolError(
            f"{backend} resource was not found.",
            error_type="not_found",
            retriable=False,
            backend=backend,
        )
    if status_code in {400, 405, 409, 422}:
        return ToolError(
            f"{backend} rejected invalid request input with HTTP {status_code}.",
            error_type="invalid_input",
            retriable=False,
            backend=backend,
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return ToolError(
            f"{backend} request timed out.",
            error_type="timeout",
            retriable=True,
            backend=backend,
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ToolError(
            f"Could not connect to {backend}.",
            error_type="connection",
            retriable=True,
            backend=backend,
        )
    if status_code is not None:
        retriable = int(status_code) >= 500
        return ToolError(
            f"{backend} returned HTTP {status_code}.",
            error_type="unavailable" if retriable else "internal",
            retriable=retriable,
            backend=backend,
        )
    return ToolError(
        f"{backend} request failed.",
        error_type="internal",
        retriable=False,
        backend=backend,
    )


def _retry_after_seconds(response: object) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    value = str(headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from datetime import datetime, timezone

            retry_at = parsedate_to_datetime(value)
            now = datetime.now(timezone.utc)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
