"""Pin the initialized CLI destination without claiming filesystem/data CAS."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy

from agent.memory_provider import OwnedToolTargetChanged

_selected = ContextVar("byterover_owned_route", default=None)


def operation_provider(provider):
    selected = _selected.get()
    return selected[1] if selected is not None and selected[0] is provider else provider


def capture_provider(provider):
    return copy(operation_provider(provider))


@contextmanager
def bind_route(provider, target):
    if target is not None:
        raise OwnedToolTargetChanged("ByteRover has no atomic data target scope")
    token = _selected.set((provider, capture_provider(provider)))
    try:
        yield
    finally:
        _selected.reset(token)
