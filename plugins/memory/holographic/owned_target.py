"""SQLite lifetime revisions and atomic scopes for explicit owned provider calls."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
import sqlite3

from agent.memory_provider import OwnedToolTargetChanged

_CURRENT = ContextVar("holographic_owned_operation", default=None)


def operation_provider(provider):
    selected = _CURRENT.get()
    return selected[1] if selected is not None and selected[0] is provider else provider


def resolve_target(provider, name, args):
    store = provider._store
    if store is None or name not in provider._TOOL_HANDLERS:
        return None
    if provider._retriever is None or provider._retriever.store is not store:
        raise OwnedToolTargetChanged("Provider retrieval destination differs from its fact store")
    with store._lock:
        if store._entry is None:
            raise OwnedToolTargetChanged("Provider database is closed")
        # SQLite data_version changes for other connections; total_changes covers
        # our shared connection, including content ABA and conservative rollback.
        revision = {"external": store._conn.execute("PRAGMA data_version").fetchone()[0],
                    "local": store._conn.total_changes,
                    "schema": store._conn.execute("PRAGMA schema_version").fetchone()[0]}
        return {"namespace": "selected-provider-memory", "provider": provider.name,
                "database": store._key, "action": args.get("action"),
                "fact_id": args.get("fact_id"), "revision": revision}


@contextmanager
def bind_target(provider, name, args, target):
    store = provider._store
    if store is None or target is None:
        raise OwnedToolTargetChanged("Provider database target is unavailable")
    with store._lock:
        if store._conn.in_transaction:
            raise OwnedToolTargetChanged("Provider database already has an active transaction")
        try:
            store._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise OwnedToolTargetChanged("Provider database could not acquire its execution fence") from exc
        token = None
        try:
            if resolve_target(provider, name, args) != target:
                raise OwnedToolTargetChanged("Provider data changed after target review")
            # Pin the real operation's mutable owners, independently of the route
            # checks. A subsequent replacement cannot redirect the selected SQL.
            selected = copy(provider)
            selected._store = copy(store)
            selected._retriever = copy(provider._retriever)
            selected._retriever.store = selected._store
            token = _CURRENT.set((provider, selected))
            store._entry["owned_transaction"] = True
            store._entry["owned_operation_error"] = False
            yield
            if store._entry["owned_operation_error"]:
                store._conn.rollback()
            else:
                store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise
        finally:
            if token is not None:
                _CURRENT.reset(token)
            store._entry.pop("owned_transaction", None)
            store._entry.pop("owned_operation_error", None)
