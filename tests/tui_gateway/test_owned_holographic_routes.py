"""Real bundled provider routes must retain the selected profile's SQLite owners."""

import copy
import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call


def opened_provider(path):
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("holographic", register_skills=False)
    assert provider is not None
    provider._config = {"db_path": str(path.parent / "facts.db"), "hrr_dim": 64}
    provider.initialize("same-durable-owner")
    return provider


def providers_and_state(sessions):
    providers = {key: session["agent"]._memory_manager.providers[0] for key, session in sessions.items()}
    stores = {key: provider._store for key, provider in providers.items()}
    frozen = {key: copy.deepcopy((session["agent"].tools, session["history"])) for key, session in sessions.items()}
    return providers, stores, frozen


def rows(stores):
    return {key: [tuple(row) for row in store._conn.execute("SELECT * FROM facts ORDER BY fact_id")]
            for key, store in stores.items()}


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
@pytest.mark.parametrize("route,name,args", [
    ("store", "fact_store", {"action": "add", "content": "owned new fact"}),
    ("store", "fact_store", {"action": "update", "fact_id": 1, "content": "owned updated fact"}),
    ("store", "fact_store", {"action": "remove", "fact_id": 1}),
    ("store", "fact_store", {"action": "list"}),
    ("store", "fact_feedback", {"action": "helpful", "fact_id": 1}),
    ("retriever", "fact_store", {"action": "search", "query": "Private", "min_trust": 0.0}),
    ("retriever", "fact_store", {"action": "probe", "entity": "Private"}),
    ("retriever", "fact_store", {"action": "related", "entity": "Private"}),
    ("retriever", "fact_store", {"action": "reason", "entities": ["Private", "Profile"]}),
    ("retriever", "fact_store", {"action": "contradict"}),
])
def test_owned_holographic_selected_database_cannot_redirect(owned_sessions, monkeypatch, stage, route, name, args):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers, stores, frozen = providers_and_state(owned_sessions)
    for key, store in stores.items():
        store.add_fact(f"Private {key} profile fact")
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        provider = providers[owner]
        target, attribute = (provider, "_store") if route == "store" else (provider._retriever, "store")
        before = rows(stores)
        selected = session_tool_snapshot(owned_sessions[owner])

        def redirect(*, args, next_call=None, **kwargs):
            target.__dict__[attribute] = stores[foreign]
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [redirect]
        request = f"holographic-{route}-{stage}-{visit}"
        try:
            result = _call(owned_sessions, owner, owner, name, args, request)["result"]
            assert result["state"] == "rejected", result
            assert result["output"] is None and rows(stores) == before
            session = owned_sessions[owner]
            receipt = session["agent"]._session_db.get_tool_attempt(session["session_key"], result["attempt_id"])
            assert receipt["dispatched_at"] is None
            changed = session_tool_snapshot(session)
            assert changed["context_id"] != selected["context_id"]
        finally:
            collection.clear()
            target.__dict__[attribute] = stores[owner]
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {selected["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, name, args, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
        assert rows(stores) == before
        fresh = _call(owned_sessions, owner, owner, name, args, f"fresh-{visit}")["result"]
        assert fresh["state"] == "returned", fresh
        assert rows(stores)[foreign] == before[foreign]
        committed = rows(stores)
        replay = _call(owned_sessions, owner, owner, name, args, f"fresh-{visit}")["result"]
        assert replay["duplicate"] and replay["output"] is None and rows(stores) == committed
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["before_builtin", "after_builtin"])
def test_owned_holographic_mirror_cannot_redirect(owned_sessions, monkeypatch, stage):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers, stores, frozen = providers_and_state(owned_sessions)
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        agent, provider = owned_sessions[owner]["agent"], providers[owner]
        original = agent._build_memory_write_metadata
        before = rows(stores)
        builtin = {key: session["agent"]._memory_store.memory_entries[:] for key, session in owned_sessions.items()}
        selected = session_tool_snapshot(owned_sessions[owner])

        def redirect(**kwargs):
            provider._store = stores[foreign]
            return original(**kwargs)

        def redirect_before(*, args, next_call, **kwargs):
            provider._store = stores[foreign]
            return next_call(args)

        args = {"action": "add", "content": f"mirror {stage} {visit}"}
        request = f"holographic-mirror-{stage}-{visit}"
        try:
            if stage == "after_builtin":
                monkeypatch.setattr(agent, "_build_memory_write_metadata", redirect)
            else:
                manager._middleware["tool_execution"] = [redirect_before]
            result = _call(owned_sessions, owner, owner, "memory", args, request)["result"]
            assert rows(stores) == before
            assert result["output"] is None, result
            if stage == "after_builtin":
                assert result["observation"] == "unknown", result
                assert args["content"] in agent._memory_store.memory_entries
            else:
                assert result["state"] == "rejected", result
                assert agent._memory_store.memory_entries == builtin[owner]
            assert owned_sessions[foreign]["agent"]._memory_store.memory_entries == builtin[foreign]
            changed = session_tool_snapshot(owned_sessions[owner])
            assert changed["context_id"] != selected["context_id"]
        finally:
            provider._store = stores[owner]
            monkeypatch.setattr(agent, "_build_memory_write_metadata", original)
            manager._middleware.clear()
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {selected["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, "memory", args, request)["result"]
        assert replay["duplicate"] and replay["output"] is None and rows(stores) == before
        fresh = _call(owned_sessions, owner, owner, "memory", {"action": "add", "content": f"fresh mirror {visit}"},
                      f"fresh-mirror-{visit}")["result"]
        assert fresh["state"] == "returned", fresh
        assert [row["content"] for row in stores[owner]._conn.execute("SELECT content FROM facts")].count(f"fresh mirror {visit}") == 1
        assert rows(stores)[foreign] == before[foreign]
        committed = rows(stores)
        replay = _call(owned_sessions, owner, owner, "memory", {"action": "add", "content": f"fresh mirror {visit}"},
                       f"fresh-mirror-{visit}")["result"]
        assert replay["duplicate"] and replay["output"] is None and rows(stores) == committed
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
