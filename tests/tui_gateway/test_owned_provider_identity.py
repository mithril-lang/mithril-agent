"""Actual provider-only owned routes retain declared identity without a built-in memory schema."""

import copy
import json

import pytest

from agent.memory_provider import MemoryProvider
from tests.tui_gateway.test_owned_tool_call import _call, owned_sessions  # noqa: F401


class ProviderCanary(MemoryProvider):
    name = "qualification-direct-provider"

    def __init__(self, path):
        self.path, self.after_write = path, None

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return [{"name": "qualification_provider_write", "description": "Write a local qualification canary.",
                 "parameters": {"type": "object", "properties": {"content": {"type": "string"}},
                                "required": ["content"]}}]

    def identity_signature(self):
        return {"destination": str(self.path)}

    def handle_tool_call(self, name, args, **kwargs):
        assert name == "qualification_provider_write"
        with self.path.open("a") as output:
            output.write(json.dumps(args) + "\n")
        if self.after_write:
            self.after_write()
        return json.dumps({"success": True, "content": args["content"]})


@pytest.mark.parametrize("owned_sessions", [ProviderCanary], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
def test_owned_provider_declared_identity_retires_consent(owned_sessions, monkeypatch, stage):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers = {k: s["agent"]._memory_manager.providers[0] for k, s in owned_sessions.items()}
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}
    assert all("memory" not in [d["function"]["name"] for d in s["agent"].tools] for s in owned_sessions.values())
    for visit, owner in enumerate(["a", "b", "a"]):
        foreign = "b" if owner == "a" else "a"
        provider = providers[owner]
        original_path = provider.path
        before = session_tool_snapshot(owned_sessions[owner])

        def redirect(*, args, next_call=None, **kwargs):
            provider.path = providers[foreign].path
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [redirect]
        args = {"content": f"refuse-provider-{visit}"}
        request = f"provider-identity-{stage}-{visit}"
        result = _call(owned_sessions, owner, owner, "qualification_provider_write", args, request)["result"]
        assert result["state"] == "rejected", result
        row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
        assert row["dispatched_at"] is None
        assert not original_path.exists() and not providers[foreign].path.exists()
        changed = session_tool_snapshot(owned_sessions[owner])
        assert changed["context_id"] != before["context_id"] and changed["revision"] == before["revision"]
        manager._hooks.clear()
        manager._middleware.clear()
        provider.path = original_path
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {before["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, "qualification_provider_write", args, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
        assert not original_path.exists() and not providers[foreign].path.exists()
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
    fresh = _call(owned_sessions, "a", "a", "qualification_provider_write", {"content": "fresh provider"}, "provider-fresh")["result"]
    assert fresh["state"] == "returned" and len(providers["a"].path.read_text().splitlines()) == 1
    assert not providers["b"].path.exists()
    replay = _call(owned_sessions, "a", "a", "qualification_provider_write", {"content": "fresh provider"}, "provider-fresh")["result"]
    assert replay["duplicate"] and len(providers["a"].path.read_text().splitlines()) == 1


@pytest.mark.parametrize("owned_sessions", [ProviderCanary], indirect=True)
def test_owned_provider_identity_after_effect_suppresses_disclosure(owned_sessions):
    from tui_gateway.tool_snapshot import session_tool_snapshot

    provider = owned_sessions["a"]["agent"]._memory_manager.providers[0]
    foreign = owned_sessions["b"]["agent"]._memory_manager.providers[0]
    original_path = provider.path
    before = session_tool_snapshot(owned_sessions["a"])
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}

    def retire():
        provider.path = foreign.path

    provider.after_write = retire
    args = {"content": "effect landed before declared identity changed"}
    result = _call(owned_sessions, "a", "a", "qualification_provider_write", args, "provider-after-write")["result"]
    assert original_path.read_text().splitlines() == [json.dumps(args)]
    assert not foreign.path.exists()
    assert result["observation"] == "unknown" and result["output"] is None, result
    assert session_tool_snapshot(owned_sessions["a"])["context_id"] != before["context_id"]
    provider.after_write, provider.path = None, original_path
    replay = _call(owned_sessions, "a", "a", "qualification_provider_write", args, "provider-after-write")["result"]
    assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
    assert original_path.read_text().splitlines() == [json.dumps(args)] and not foreign.path.exists()
    for key, session in owned_sessions.items():
        assert (session["agent"].tools, session["history"]) == frozen[key]
