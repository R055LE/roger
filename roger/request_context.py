"""Task-local request correlation for inbound admin and ambient interactions."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_context() -> Iterator[str]:
    """Bind one opaque ID for the current task, then always restore its previous value."""
    request_id = secrets.token_hex(8)
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)
