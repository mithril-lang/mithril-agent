"""Pin initialized HTTP routing; no remote transaction or data CAS promise."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy

from agent.memory_provider import OwnedToolTargetChanged

_selected = ContextVar("retaindb_owned_route", default=None)


def operation_provider(provider):
    selected = _selected.get()
    return selected[1] if selected is not None and selected[0] is provider else provider


@contextmanager
def bind_route(provider, target):
    if target is not None:
        raise OwnedToolTargetChanged("RetainDB has no atomic remote target scope")
    selected = copy(provider)
    selected._client = copy(provider._client)
    token = _selected.set((provider, selected))
    try:
        yield
    finally:
        _selected.reset(token)
