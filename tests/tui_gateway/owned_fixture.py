"""Shared real profile sessions for bounded owned-tool qualification."""

import copy
import json
import queue
from pathlib import Path
import threading
from unittest.mock import patch

import pytest


class WireStream:
    def __init__(self):
        self.replies = queue.Queue()

    def write(self, line):
        frame = json.loads(line)
        if "result" in frame or "error" in frame:
            self.replies.put(frame)
        return len(line)

    def flush(self):
        pass


@pytest.fixture
def owned_sessions(tmp_path, monkeypatch, request):
    import tools.file_tools  # noqa: F401
    import tools.todo_tool  # noqa: F401
    import tools.code_execution_tool  # noqa: F401
    import tui_gateway.server as server
    from agent import secret_scope
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.registry import registry
    from tui_gateway.transport import StdioTransport

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    sessions = {}
    names = ["read_file", "write_file", "todo_list", "execute_code"]
    if getattr(request.node, "originalname", None) in {
        "test_owned_search_root_target_and_replay", "test_owned_search_defaults_stay_in_selected_profile",
        "test_compiled_owned_sdk_real_stdio_roundtrip",
    }:
        names.append("search_files")
    if getattr(request.node, "originalname", None) in {
        "test_owned_patch_targets_preserve_content_and_entries", "test_compiled_owned_sdk_real_stdio_roundtrip",
        "test_real_browser_api_owned_hermes_network",
    }:
        names.append("patch")
    inline_state = getattr(request.node, "originalname", None) in {
        "test_owned_memory_preserves_profile_prompt_and_replay",
        "test_owned_session_search_uses_attached_durable_store",
        "test_owned_inline_store_replacement_retires_approval",
        "test_owned_memory_mirror_route_change_retires_consent",
        "test_owned_memory_metadata_cannot_redirect_mirror_after_builtin_write",
        "test_owned_memory_target_preserves_store_mirrors_and_replay",
        "test_owned_memory_reviewed_routes_cannot_be_substituted",
        "test_owned_pending_failure_never_applies_or_replays_memory",
        "test_owned_staged_memory_keeps_reviewed_route_until_explicit_approval",
        "test_real_browser_api_owned_hermes_network",
    }
    if inline_state:
        import tools.memory_tool  # noqa: F401
        import tools.session_search_tool  # noqa: F401
        names += ["memory", "session_search"]
    if request.node.name == "test_owned_runtime_generation_shared_by_file_terminal_and_remote_code_resolver":
        names.append("terminal")
    definitions = [{"type": "function", "function": copy.deepcopy(registry.get_entry(name).schema)} for name in names]
    collision_fixture = getattr(request.node, "originalname", None) == "test_real_browser_api_owned_hermes_network"
    provider_class = getattr(request, "param", None)
    if collision_fixture and request.node.callspec.params["family"] in {"inline", "staged", "staged-review"}:
        from tests.tui_gateway.test_owned_provider_identity import ProviderCanary
        provider_class = ProviderCanary
    if collision_fixture:
        monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
        monkeypatch.setattr(registry, "_generation", registry._generation)
    for name in ["a", "b"]:
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("code_execution:\n  mode: strict\n  timeout: 20\n")
        profile_definitions = copy.deepcopy(definitions)
        if collision_fixture and name == "a":
            # Publish fixture collision schemas before constructing the agent's
            # frozen prefix. Real file handler; no real Web provider is claimed.
            read = registry.get_entry("read_file")
            for wire in ["web_search", "web_extract"]:
                schema = copy.deepcopy(read.schema)
                schema["name"] = wire
                registry.register(name=wire, toolset="qualification-file-alias", schema=schema,
                                  handler=read.handler, check_fn=read.check_fn,
                                  scope=str(home), override=True)
                profile_definitions.append({"type": "function", "function": schema})
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            provider_manager = None
            if provider_class:
                from agent.memory_manager import MemoryManager
                provider_manager = MemoryManager()
                provider_manager.add_provider(provider_class(home / "provider-canary.jsonl"))
                profile_definitions += [{"type": "function", "function": schema}
                                        for schema in provider_manager.get_all_tool_schemas()]
            with (patch("model_tools.get_tool_definitions", return_value=copy.deepcopy(profile_definitions)),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("agent.process_bootstrap.OpenAI"),
                  patch("agent.model_metadata.fetch_model_metadata", return_value={})):
                agent = AIAgent(api_key="test-key", base_url="https://example.invalid",
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    enabled_toolsets=["memory"] if inline_state else None)
            if provider_manager is not None:
                agent._memory_manager = provider_manager
            db = SessionDB(db_path=home / "state.db")
            db.create_session(session_id="same-durable-owner", source="test", model="test")
            agent.session_id, agent._session_db = "same-durable-owner", db
            agent._session_messages = [{"role": "user", "content": "owned conversation"}]
            wire = WireStream()
            sessions[name] = {"agent": agent, "profile_home": str(home), "session_key": agent.session_id,
                "history_lock": threading.RLock(), "running": False, "history": agent._session_messages,
                "cwd": str(home), "wire": wire,
                "transport": StdioTransport(lambda wire=wire: wire, threading.Lock())}
    monkeypatch.setattr(server, "_sessions", sessions)
    yield sessions
    from tools.code_kernel import shutdown_kernels_for_owner
    for session in sessions.values():
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            shutdown_kernels_for_owner("tool-only:same-durable-owner")
            agent = session["agent"]
            db = agent._session_db
            # The real close finalizes its row. Retire it before closing the
            # borrowed fixture DB, then detach it so GC cannot reopen the handle.
            agent.close()
            agent._session_db = None
        db.close()

