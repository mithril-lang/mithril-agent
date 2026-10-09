"""Real plugin discovery forwards metadata through profile-owned registration."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_plugin(home, *, coverage="partial"):
    directory = home / "plugins" / "manifest_probe"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        "name: manifest_probe\nversion: 0.1.0\ndescription: local manifest qualification\n")
    (home / "config.yaml").write_text("plugins:\n  enabled: [manifest_probe]\n")
    (directory / "__init__.py").write_text(
        "from copy import deepcopy\n"
        "from tools.file_tools import WRITE_FILE_SCHEMA, _handle_write_file\n"
        f"MANIFEST = {{'coverage': {coverage!r}, 'effects': ['file.write'], 'targets': ["
        "{'kind': 'file-path', 'argument': '/path', 'resolution': 'selected-terminal-runtime'}]}\n"
        "def write(args, **kwargs):\n"
        "    return _handle_write_file(args, **kwargs)\n"
        "def register(ctx):\n"
        "    schema = deepcopy(WRITE_FILE_SCHEMA)\n"
        "    schema['name'] = 'manifest_probe_write'\n"
        "    ctx.register_tool(name='manifest_probe_write', toolset='manifest_probe',\n"
        "                      schema=schema, handler=write, effect_manifest=MANIFEST)\n")


def _isolate(monkeypatch, tmp_path):
    from agent import secret_scope
    from hermes_cli.plugins import PluginManager

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setattr("hermes_cli.plugins.get_bundled_plugins_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])


@pytest.mark.platforms("posix")
def test_discovered_plugin_manifest_profile_dispatch_reload_and_unload(tmp_path, monkeypatch):
    import tui_gateway.server as server
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    from tui_gateway.tool_snapshot import session_tool_snapshot
    from run_agent import AIAgent

    _isolate(monkeypatch, tmp_path)
    sessions, managers = {}, {}
    for owner in ("a", "b"):
        home = tmp_path / owner
        _write_plugin(home)
        session = {"profile_home": str(home), "cwd": str(home)}
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            manager = PluginManager(scope_key=str(home))
            managers[owner] = manager
            manager.discover_and_load()
            entry = registry.get_entry("manifest_probe_write")
            assert entry is not None, manager._plugins
            definitions = [{"type": "function", "function": copy.deepcopy(entry.schema)}]
            with (patch("model_tools.get_tool_definitions", return_value=definitions),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
                  patch("agent.process_bootstrap.OpenAI"),
                  patch("agent.model_metadata.fetch_model_metadata", return_value={})):
                session["agent"] = AIAgent(api_key="test-key", base_url="https://example.invalid",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
            session["agent"].session_id = "plugin-manifest-owner"
            module = manager._plugins["manifest_probe"].module
            module.MANIFEST["effects"].append("caller-mutation")
            assert entry.effect_manifest["effects"] == ["file.write"]
        sessions[owner] = session
    try:
        first, foreign = session_tool_snapshot(sessions["a"]), session_tool_snapshot(sessions["b"])
        for owner in ("a", "b", "a"):
            session = sessions[owner]
            path = Path(session["profile_home"]) / "effect.txt"
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                result = json.loads(registry.dispatch("manifest_probe_write",
                    {"path": str(path), "content": owner}, task_id="plugin-manifest-owner"))
            assert "error" not in result and path.read_text() == owner, result
        with server._session_profile_runtime_scope(sessions["a"], hydrate_secrets=False):
            managers["a"].discover_and_load(force=True)
        reloaded = session_tool_snapshot(sessions["a"])
        assert reloaded["effect_manifests"] == first["effect_manifests"]
        assert reloaded["revision"] == first["revision"] and reloaded["context_id"] != first["context_id"]
        assert session_tool_snapshot(sessions["b"]) == foreign
        from hermes_cli.plugins import PluginContext, PluginManifest
        with server._session_profile_runtime_scope(sessions["a"], hydrate_secrets=False):
            original_entry = registry.get_entry("manifest_probe_write")
            context = PluginContext(PluginManifest(name="manifest_overlay", key="manifest_overlay"), managers["a"])
            metadata = copy.deepcopy(original_entry.effect_manifest)
            metadata["effects"].append("file.metadata")
            overlay = context.register_tool("manifest_probe_write", original_entry.toolset,
                original_entry.schema, original_entry.handler, effect_manifest=metadata)
            assert overlay is not None
        overlaid = session_tool_snapshot(sessions["a"])
        assert "file.metadata" in overlaid["effect_manifests"][0]["effects"]
        with server._session_profile_runtime_scope(sessions["a"], hydrate_secrets=False):
            overlay.dispose()
            assert registry.get_entry("manifest_probe_write") is original_entry
        restored = session_tool_snapshot(sessions["a"])
        assert restored["effect_manifests"] == reloaded["effect_manifests"]
        assert restored["context_id"] != reloaded["context_id"]
        assert session_tool_snapshot(sessions["b"]) == foreign
        with server._session_profile_runtime_scope(sessions["a"], hydrate_secrets=False):
            managers["a"].unload("manifest_probe")
            assert registry.get_entry("manifest_probe_write") is None
        retired = session_tool_snapshot(sessions["a"])
        assert retired["effect_manifests"][0]["coverage"] == "unknown"
        assert retired["context_id"] != reloaded["context_id"]
        assert session_tool_snapshot(sessions["b"]) == foreign
    finally:
        for owner, manager in managers.items():
            with server._session_profile_runtime_scope(sessions[owner], hydrate_secrets=False):
                manager.unload("manifest_probe")
                sessions[owner]["agent"].close()


def test_invalid_plugin_manifest_load_fails_without_registry_or_ledger_residue(tmp_path, monkeypatch):
    import tui_gateway.server as server
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    _isolate(monkeypatch, tmp_path)
    home = tmp_path / "invalid"
    _write_plugin(home, coverage="complete")
    with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
        manager = PluginManager(scope_key=str(home))
        manager.discover_and_load()
        assert registry.get_entry("manifest_probe_write") is None
        assert "manifest_probe_write" not in manager._plugin_tool_names
        assert not manager._ownership_ledger.get("manifest_probe")
        assert "partial coverage" in manager._plugins["manifest_probe"].error
