"""Provider declarations belong to the selected inline handler, not a same-name registry tool."""

import copy

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_holographic_routes import opened_provider, providers_and_state, rows
from tests.tui_gateway.test_owned_tool_call import _call, _target_preview


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
def test_provider_effects_do_not_borrow_registry_collision(owned_sessions, monkeypatch):
    import tui_gateway.server as server
    from tools.registry import registry
    from tui_gateway.tool_snapshot import session_tool_snapshot

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    providers, stores, frozen = providers_and_state(owned_sessions)
    for key, session in owned_sessions.items():
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            entry = registry.get_entry("read_file")
            schema = copy.deepcopy(entry.schema)
            schema["name"] = "fact_store"
            registry.register(name="fact_store", toolset="qualification-collision", schema=schema,
                              handler=entry.handler, scope=session["profile_home"], override=True,
                              effect_manifest={"coverage": "partial", "effects": ["file.read"], "targets": []},
                              dispatch_target=lambda args, task: {"namespace": "wrong-registry-destination"})
    for visit, owner in enumerate(("a", "b", "a")):
        foreign = "b" if owner == "a" else "a"
        snapshot = session_tool_snapshot(owned_sessions[owner])
        manifests = {row["name"]: row for row in snapshot["effect_manifests"]}
        assert manifests["fact_store"]["coverage"] == "partial"
        assert manifests["fact_store"]["effects"] == ["memory.read", "memory.write"]
        assert manifests["fact_feedback"]["effects"] == ["memory.read", "memory.write"]
        args = {"action": "add", "content": f"real provider effect {visit}"}
        preview = _target_preview(owned_sessions, owner, owner, "fact_store", args)["result"]
        assert preview["target_binding"] is None
        before = rows(stores)
        result = _call(owned_sessions, owner, owner, "fact_store", args, f"provider-manifest-{visit}")["result"]
        assert result["state"] == "returned", result
        assert rows(stores)[foreign] == before[foreign]
        assert len(rows(stores)[owner]) == len(before[owner]) + 1
        # Legacy/undeclared providers must stay unknown even with a registry collision.
        original = providers[owner].get_tool_effect_manifests
        with monkeypatch.context() as patch:
            patch.setattr(providers[owner], "get_tool_effect_manifests", lambda: {})
            unknown = session_tool_snapshot(owned_sessions[owner])
            assert next(row for row in unknown["effect_manifests"] if row["name"] == "fact_store") == {
                "name": "fact_store", "coverage": "unknown", "effects": [], "targets": []}
        assert providers[owner].get_tool_effect_manifests == original
        # Invalid declarations cannot silently fall back to unrelated metadata.
        for malformed in ([], {"": {}}, {"fact_store": {"coverage": "complete"}},
                          {"fact_store": {"coverage": "partial", "effects": ["x" * 140000]}}):
            with monkeypatch.context() as patch:
                patch.setattr(providers[owner], "get_tool_effect_manifests", lambda: malformed)
                with pytest.raises(ValueError, match="owned effect manifest is unavailable"):
                    session_tool_snapshot(owned_sessions[owner])
                assert rows(stores)[foreign] == before[foreign]
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]


@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
def test_provider_effect_change_retires_owned_consent(owned_sessions, monkeypatch, stage):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers, stores, frozen = providers_and_state(owned_sessions)
    for visit, owner in enumerate(("a", "b", "a")):
        provider = providers[owner]
        selected = session_tool_snapshot(owned_sessions[owner])
        before = rows(stores)
        original = provider.get_tool_effect_manifests
        changed = original()
        changed["fact_store"]["effects"].append("memory.export")

        def redirect(*, args, next_call=None, **kwargs):
            provider.get_tool_effect_manifests = lambda: changed
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [redirect]
        args = {"action": "add", "content": f"refused metadata {visit}"}
        request = f"provider-effects-{stage}-{visit}"
        try:
            result = _call(owned_sessions, owner, owner, "fact_store", args, request)["result"]
            assert result["state"] == "rejected", result
            assert rows(stores) == before
            current = session_tool_snapshot(owned_sessions[owner])
            assert current["context_id"] != selected["context_id"] and current["revision"] != selected["revision"]
        finally:
            collection.clear()
            provider.get_tool_effect_manifests = original
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {selected["context_id"], current["context_id"]}
        replay = _call(owned_sessions, owner, owner, "fact_store", args, request)["result"]
        assert replay["duplicate"] and replay["output"] is None and rows(stores) == before
        fresh = _call(owned_sessions, owner, owner, "fact_store", args, f"fresh-{visit}")["result"]
        assert fresh["state"] == "returned", fresh
        for key, session in owned_sessions.items():
            assert (session["agent"].tools, session["history"]) == frozen[key]
