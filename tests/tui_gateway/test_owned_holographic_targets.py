"""Actual provider target revisions survive review, middleware and SQLite contention."""

import sqlite3
import subprocess
import sys
from pathlib import Path
import threading

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_holographic_routes import opened_provider, providers_and_state, rows
from tests.tui_gateway.test_owned_tool_call import _call, _target_preview


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
@pytest.mark.parametrize("name,args", [
    ("fact_store", {"action": "add", "content": "owned new target"}),
    ("fact_store", {"action": "update", "fact_id": 1, "content": "owned update"}),
    ("fact_store", {"action": "remove", "fact_id": 1}),
    ("fact_store", {"action": "list"}),
    ("fact_store", {"action": "search", "query": "Private", "min_trust": 0.0}),
    ("fact_store", {"action": "probe", "entity": "Private"}),
    ("fact_store", {"action": "related", "entity": "Private"}),
    ("fact_store", {"action": "reason", "entities": ["Private", "Profile"]}),
    ("fact_store", {"action": "contradict"}),
    ("fact_feedback", {"action": "helpful", "fact_id": 1}),
    ("fact_feedback", {"action": "unhelpful", "fact_id": 1}),
])
def test_holographic_reviewed_revision_retires_before_effect(owned_sessions, monkeypatch, stage, name, args):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    _, stores, frozen = providers_and_state(owned_sessions)
    for owner, store in stores.items():
        store.add_fact(f"Private {owner} Profile")
    for visit, owner in enumerate(("a", "b", "a")):
        foreign = "b" if owner == "a" else "a"
        selected = session_tool_snapshot(owned_sessions[owner])
        preview = _target_preview(owned_sessions, owner, owner, name, args)["result"]["target_binding"]
        assert preview["target"]["namespace"] == "selected-provider-memory"
        assert preview["target"]["database"] == stores[owner]._key
        assert preview["target"]["action"] == args["action"]
        before = rows(stores)
        modified = {}

        def race(*, args, next_call=None, **kwargs):
            # Independent SQLite writer, including a content ABA. A state hash
            # alone would incorrectly revive the reviewed data after restoration.
            with sqlite3.connect(stores[owner]._key) as writer:
                writer.execute("UPDATE facts SET tags = 'changed' WHERE fact_id = 1")
            with sqlite3.connect(stores[owner]._key) as writer:
                writer.execute("UPDATE facts SET tags = '' WHERE fact_id = 1")
            modified.update(rows(stores))
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [race]
        request = f"provider-target-{visit}"
        try:
            result = _call(owned_sessions, owner, owner, name, args, request,
                           target_digest=preview["digest"])["result"]
            assert result["state"] == "rejected" and result["output"] is None, result
            assert rows(stores) == modified == before
            db = owned_sessions[owner]["agent"]._session_db
            assert db.get_tool_attempt("same-durable-owner", result["attempt_id"])["dispatched_at"] is None
        finally:
            collection.clear()
        assert session_tool_snapshot(owned_sessions[owner]) == selected
        fresh = _target_preview(owned_sessions, owner, owner, name, args)["result"]["target_binding"]
        assert fresh["digest"] != preview["digest"]
        duplicate = _call(owned_sessions, owner, owner, name, args, request,
                          target_digest=preview["digest"])["result"]
        assert duplicate["duplicate"] and duplicate["output"] is None and rows(stores) == before
        allowed = _call(owned_sessions, owner, owner, name, args, f"fresh-{visit}",
                        target_digest=fresh["digest"])["result"]
        assert allowed["state"] == "returned" and allowed["observation"] == "handler-return", allowed
        assert rows(stores)[foreign] == before[foreign]
        committed = rows(stores)
        repeated = _call(owned_sessions, owner, owner, name, args, f"fresh-{visit}",
                         target_digest=fresh["digest"])["result"]
        assert repeated["duplicate"] and repeated["output"] is None and rows(stores) == committed
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
        # Restore a seed for the next A visit after remove; this is fixture work.
        if args["action"] == "remove":
            stores[owner]._conn.execute("INSERT INTO facts(fact_id,content) VALUES (1,?)", (f"Private {owner} Profile",))


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("boundary", ["rollback", "competing-process", "route-pin"])
def test_holographic_owned_transaction_preserves_all_or_none(owned_sessions, monkeypatch, boundary, tmp_path):
    providers, stores, frozen = providers_and_state(owned_sessions)
    for visit, owner in enumerate(("a", "b", "a")):
        provider, store = providers[owner], stores[owner]
        args = {"action": "add", "content": f"Private Atomic Profile {owner} {visit}"}
        target = provider.resolve_owned_tool_target("fact_store", args)
        before = rows(stores)
        if boundary == "rollback":
            with monkeypatch.context() as patch:
                patch.setattr(store, "_compute_hrr_vector", lambda *args: (_ for _ in ()).throw(RuntimeError("vector failure")))
                result = _call(owned_sessions, owner, owner, "fact_store", args, f"rollback-{visit}")["result"]
            assert result["state"] == "returned-error", result
            assert rows(stores) == before
            with store._lock:
                assert store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
                assert store._conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0] == 0
            assert not store._conn.in_transaction
        elif boundary == "route-pin":
            foreign = "b" if owner == "a" else "a"
            with provider.bind_owned_tool_target("fact_store", args, target):
                provider._store = stores[foreign]
                try:
                    output = provider.handle_tool_call("fact_store", args)
                    assert "added" in output
                finally:
                    provider._store = store
            assert rows(stores)[foreign] == before[foreign]
            assert len(rows(stores)[owner]) == len(before[owner]) + 1
        else:
            started, done = tmp_path / f"started-{visit}", tmp_path / f"done-{visit}"
            code = ("import sqlite3,sys; from pathlib import Path; "
                    "c=sqlite3.connect(sys.argv[1],timeout=5); Path(sys.argv[2]).touch(); "
                    "c.execute(\"INSERT INTO facts(content) VALUES ('competing writer')\"); c.commit(); "
                    "Path(sys.argv[3]).touch()")
            with provider.bind_owned_tool_target("fact_store", args, target):
                child = subprocess.Popen([sys.executable, "-c", code, store._key, str(started), str(done)],
                                         cwd=str(Path(__file__).resolve().parents[2]))
                try:
                    import time
                    deadline = time.monotonic() + 3
                    while not started.exists() and time.monotonic() < deadline:
                        threading.Event().wait(0.02)
                    assert started.exists() and not done.exists()
                    output = provider.handle_tool_call("fact_store", args)
                    assert "added" in output and not done.exists()
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
            assert child.wait(timeout=5) == 0 and done.exists()
            assert rows(stores)["b" if owner == "a" else "a"] == before["b" if owner == "a" else "a"]
            with sqlite3.connect(store._key) as cleanup:
                cleanup.execute("DELETE FROM facts WHERE content = 'competing writer'")
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
