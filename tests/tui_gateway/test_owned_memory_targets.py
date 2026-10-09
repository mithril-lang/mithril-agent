"""Reviewed built-in and mirror identities through actual owned memory dispatch."""

import copy
import json
from pathlib import Path

import pytest

from tests.tui_gateway.test_owned_memory_mirrors import install_mirrors
from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call, _target_preview


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("batch", [False, True])
def test_owned_memory_target_preserves_store_mirrors_and_replay(owned_sessions, target, batch):
    providers = install_mirrors(owned_sessions)
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        content = f"reviewed-{target}-{batch}-{visit}"
        args = {"target": target, "operations": [{"action": "add", "content": content},
                                                 {"action": "add", "content": content + " extra"}]} if batch else {
            "action": "add", "target": target, "content": content}
        preview = _target_preview(owned_sessions, owner, owner, "memory", args)["result"]["target_binding"]
        assert preview is not None
        destination = preview["target"]
        assert set(destination) == {"namespace", "sessionId", "ownerDigest", "store", "storeDigest", "mirrors"}
        assert destination["namespace"] == "selected-session-memory" and destination["store"] == target
        assert destination["sessionId"] == "same-durable-owner"
        assert len(destination["ownerDigest"]) == len(destination["storeDigest"]) == 64
        assert destination["mirrors"] == [{"name": providers[owner].name,
                                           "identityDigest": destination["mirrors"][0]["identityDigest"]}]
        assert len(destination["mirrors"][0]["identityDigest"]) == 64
        assert str(Path(owned_sessions[owner]["profile_home"])) not in json.dumps(destination)
        other = _target_preview(owned_sessions, foreign, foreign, "memory", args)["result"]["target_binding"]
        assert other["target"]["ownerDigest"] != destination["ownerDigest"]
        assert other["target"]["storeDigest"] != destination["storeDigest"]
        assert other["target"]["mirrors"] != destination["mirrors"]
        request = f"reviewed-memory-{visit}"
        before_foreign = (providers[foreign].path.read_bytes() if providers[foreign].path.exists() else None)
        refused = _call(owned_sessions, foreign, owner, "memory", args, "foreign-" + request, target_digest=preview["digest"])
        assert refused["error"]["code"] == 4001
        refused = _call(owned_sessions, owner, owner, "memory", args, "wrong-target-" + request, target_digest=other["digest"])
        assert refused["error"]["code"] == 4092
        before = providers[owner].path.read_text().splitlines() if providers[owner].path.exists() else []
        result = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=preview["digest"])["result"]
        assert result["state"] == "returned" and result["observation"] == "handler-return", result
        store = owned_sessions[owner]["agent"]._memory_store
        assert content in store._entries_for(target)
        after = providers[owner].path.read_text().splitlines()
        assert len(after) == len(before) + (2 if batch else 1)
        assert all(json.loads(line)["target"] == target for line in after[len(before):])
        replay = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=preview["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None and replay["observation"] == "metadata-only"
        assert providers[owner].path.read_text().splitlines() == after
        assert (providers[foreign].path.read_bytes() if providers[foreign].path.exists() else None) == before_foreign
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]


@pytest.mark.parametrize("route", ["provider", "path", "disabled"])
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
def test_owned_memory_reviewed_routes_cannot_be_substituted(owned_sessions, monkeypatch, route, stage):
    from hermes_cli.plugins import PluginManager
    from hermes_constants import get_hermes_home
    from tools import memory_tool
    from tui_gateway.tool_snapshot import session_tool_snapshot

    providers = install_mirrors(owned_sessions)
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        agent = owned_sessions[owner]["agent"]
        original_path, original_dir = providers[owner].path, memory_tool.get_memory_dir
        before = session_tool_snapshot(owned_sessions[owner])
        args = {"target": "memory", "action": "add", "content": f"never redirected-{visit}"}
        reviewed = _target_preview(owned_sessions, owner, owner, "memory", args)["result"]["target_binding"]
        assert reviewed is not None

        def redirect(*, args, next_call=None, **kwargs):
            if route == "provider":
                providers[owner].path = providers[foreign].path
            elif route == "disabled":
                agent._memory_store.memory_enabled = False
            else:
                monkeypatch.setattr(memory_tool, "get_memory_dir", lambda:
                    Path(owned_sessions[foreign]["profile_home"]) / "memories"
                    if get_hermes_home() == Path(owned_sessions[owner]["profile_home"]) else original_dir())
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        (manager._hooks if stage == "pre_tool_call" else manager._middleware)[stage] = [redirect]
        request = f"memory-bound-route-{route}-{stage}-{visit}"
        result = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=reviewed["digest"])["result"]
        assert result["state"] == "rejected", result
        row = agent._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
        assert row["dispatched_at"] is None
        assert all(not p.path.exists() for p in providers.values()) and not original_path.exists()
        assert all(s["agent"]._memory_store.memory_entries == [] for s in owned_sessions.values())
        changed = session_tool_snapshot(owned_sessions[owner])
        assert changed["context_id"] != before["context_id"]
        manager._hooks.clear()
        manager._middleware.clear()
        providers[owner].path = original_path
        agent._memory_store.memory_enabled = True
        monkeypatch.setattr(memory_tool, "get_memory_dir", original_dir)
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {before["context_id"], changed["context_id"]}
        fresh = _target_preview(owned_sessions, owner, owner, "memory", args)["result"]["target_binding"]
        assert fresh["target"] == reviewed["target"] and fresh["digest"] != reviewed["digest"]
        stale = _call(owned_sessions, owner, owner, "memory", args, "stale-" + request, target_digest=reviewed["digest"])
        assert stale["error"]["code"] == 4092
        replay = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=reviewed["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
