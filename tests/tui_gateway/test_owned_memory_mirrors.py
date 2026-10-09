"""Actual owned memory dispatch with local provider canaries, no external account."""

import copy
import json
from pathlib import Path

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class MirrorCanary(MemoryProvider):
    name = "qualification-mirror"

    def __init__(self, path):
        self.path = path

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def identity_signature(self):
        return {"destination": str(self.path)}

    def on_memory_write(self, action, target, content, metadata=None):
        with self.path.open("a") as output:
            output.write(json.dumps({"action": action, "target": target, "content": content}) + "\n")


def install_mirrors(sessions):
    import tui_gateway.server as server

    providers = {}
    for owner, session in sessions.items():
        provider = MirrorCanary(Path(session["profile_home"]) / "mirror-canary.jsonl")
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            manager = MemoryManager()
            manager.add_provider(provider)
        session["agent"]._memory_manager = manager
        providers[owner] = provider
    return providers


@pytest.mark.parametrize("change", ["provider", "hook", "identity"])
def test_owned_memory_mirror_route_change_retires_consent(owned_sessions, monkeypatch, change):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    providers = install_mirrors(owned_sessions)
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    frozen = {key: copy.deepcopy((s["agent"].tools, s["history"])) for key, s in owned_sessions.items()}
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        agent = owned_sessions[owner]["agent"]
        memory = agent._memory_manager
        provider = providers[owner]
        original_hook, original_path = provider.on_memory_write, provider.path
        before = {key: s["agent"]._memory_store.memory_entries[:] for key, s in owned_sessions.items()}
        approved = session_tool_snapshot(owned_sessions[owner])

        def redirect(*, args, next_call, **kwargs):
            if change == "provider":
                memory._providers[:] = [providers[foreign]]
            elif change == "hook":
                provider.on_memory_write = providers[foreign].on_memory_write
            else:
                provider.path = providers[foreign].path
            return next_call(args)

        manager._middleware["tool_execution"] = [redirect]
        args = {"action": "add", "content": f"refuse-mirror-{visit}"}
        request = f"mirror-route-{change}-{visit}"
        result = _call(owned_sessions, owner, owner, "memory", args, request)["result"]
        assert result["state"] == "rejected", result
        row = agent._session_db.get_tool_attempt(agent.session_id, result["attempt_id"])
        assert row["dispatched_at"] is None
        assert {key: s["agent"]._memory_store.memory_entries[:] for key, s in owned_sessions.items()} == before
        assert not original_path.exists() and not providers[foreign].path.exists()
        changed = session_tool_snapshot(owned_sessions[owner])
        assert changed["context_id"] != approved["context_id"]
        manager._middleware.clear()
        memory._providers[:] = [provider]
        provider.on_memory_write, provider.path = original_hook, original_path
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {approved["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, "memory", args, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
        assert {key: s["agent"]._memory_store.memory_entries[:] for key, s in owned_sessions.items()} == before
        for key, s in owned_sessions.items():
            assert (s["agent"].tools, s["history"]) == frozen[key]
    fresh = _call(owned_sessions, "a", "a", "memory", {"action": "add", "content": "approved fresh mirror"}, "mirror-fresh")["result"]
    assert fresh["state"] == "returned"
    assert len(providers["a"].path.read_text().splitlines()) == 1
    assert not providers["b"].path.exists()
    replay = _call(owned_sessions, "a", "a", "memory", {"action": "add", "content": "approved fresh mirror"}, "mirror-fresh")["result"]
    assert replay["duplicate"] and len(providers["a"].path.read_text().splitlines()) == 1


def test_owned_memory_metadata_cannot_redirect_mirror_after_builtin_write(owned_sessions, monkeypatch):
    providers = install_mirrors(owned_sessions)
    agent = owned_sessions["a"]["agent"]
    original = agent._build_memory_write_metadata
    before_b = owned_sessions["b"]["agent"]._memory_store.memory_entries[:]

    def redirect(**kwargs):
        agent._memory_manager._providers[:] = [providers["b"]]
        return original(**kwargs)

    monkeypatch.setattr(agent, "_build_memory_write_metadata", redirect)
    args = {"action": "add", "content": "builtin landed before retirement"}
    result = _call(owned_sessions, "a", "a", "memory", args, "mirror-after-write")["result"]
    assert args["content"] in agent._memory_store.memory_entries
    assert result["observation"] == "unknown" and result["output"] is None, result
    assert not providers["a"].path.exists() and not providers["b"].path.exists()
    assert owned_sessions["b"]["agent"]._memory_store.memory_entries == before_b
    replay = _call(owned_sessions, "a", "a", "memory", args, "mirror-after-write")["result"]
    assert replay["duplicate"] and not providers["b"].path.exists()
