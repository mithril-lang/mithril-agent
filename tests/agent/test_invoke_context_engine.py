"""Single-call dispatch reaches the owning context engine without model inference."""

import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import invoke_tool
from agent.context_engine import ContextEngine
from hermes_constants import get_hermes_home


class FileContextEngine(ContextEngine):
    @property
    def name(self):
        return "file-context"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None, **kwargs):
        return messages

    def handle_tool_call(self, name, args, **kwargs):
        # This represents plugin-owned state: no registry registration or provider call.
        messages = kwargs["messages"]
        assert messages is self.expected_messages
        self.calls.append((name, args))
        return json.dumps({"owner": (get_hermes_home() / "context-owner.txt").read_text(),
                           "message": messages[-1]["content"]})


def test_single_call_engine_dispatch_preserves_live_messages_and_profile(tmp_path, monkeypatch):
    import tui_gateway.server as server
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    messages = [{"role": "user", "content": "recover this conversation"}]
    engine = FileContextEngine()
    engine.expected_messages = messages
    engine.calls = []
    agent = SimpleNamespace(session_id="owned-session", _memory_manager=None,
        _context_engine_tool_names={"recover_owned_context"}, context_compressor=engine,
        valid_tool_names={"recover_owned_context"}, enabled_toolsets=[], disabled_toolsets=[])
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        (home / "context-owner.txt").write_text(home.name)

    for home in [homes[0], homes[1], homes[0]]:
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            result = json.loads(invoke_tool(agent, "recover_owned_context", {"query": "same"},
                "owned-task", tool_call_id="owned-call", messages=messages))
            assert result == {"owner": home.name, "message": messages[-1]["content"]}
    assert engine.calls == [("recover_owned_context", {"query": "same"})] * 3
    assert messages == [{"role": "user", "content": "recover this conversation"}]
    # Retiring the engine's exposed name must not leave a cached dispatch grant.
    agent._context_engine_tool_names = set()
    with server._session_profile_runtime_scope({"profile_home": str(homes[0])}, hydrate_secrets=False):
        retired = json.loads(invoke_tool(agent, "recover_owned_context", {}, "owned-task", messages=messages))
    assert "error" in retired
    assert len(engine.calls) == 3
