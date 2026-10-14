"""Shared cooperative-cancellation primitives for live operations."""

from __future__ import annotations

import inspect
from typing import Protocol


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class OperationCancelled(RuntimeError):
    """A caller requested a clean stop of an in-progress live operation."""


def validate_cancel_event(cancel_event: CancellationToken | None) -> None:
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise TypeError("cancel_event must provide an is_set() method or be None")


def raise_if_cancelled(cancel_event: CancellationToken | None) -> None:
    if cancel_event is not None and bool(cancel_event.is_set()):
        raise OperationCancelled("operation cancelled by user")


def start_transport(transport: object, cancel_event: CancellationToken | None) -> object:
    """Start a transport, passing cancellation only when its API supports it."""
    validate_cancel_event(cancel_event)
    start = getattr(transport, "start", None)
    if not callable(start):
        raise TypeError("transport must provide a start() method")
    if cancel_event is None:
        return start()
    try:
        supports_cancel = "cancel_event" in inspect.signature(start).parameters
    except (TypeError, ValueError):
        supports_cancel = False
    if supports_cancel:
        return start(cancel_event=cancel_event)
    return start()


__all__ = [
    "CancellationToken",
    "OperationCancelled",
    "raise_if_cancelled",
    "start_transport",
    "validate_cancel_event",
]
