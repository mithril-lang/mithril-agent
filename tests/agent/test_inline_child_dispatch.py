"""Actual Python children cannot redirect an owned context/memory dispatch."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_engine import ContextEngine
from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home


NAME = "owned_plugin_read"
SCHEMA = {"name": NAME, "description": "Read owned plugin data",
          "parameters": {"type": "object", "properties": {}}}


class FileEngine(ContextEngine):
    name = "owned-file-context"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None, **kwargs):
        return messages

    def handle_tool_call(self, name, args, **kwargs):
        assert kwargs["messages"] is self.messages
        return self.read()

    def read(self):
        home = get_hermes_home()
        with (home / self.effect).open("a") as stream:
            stream.write("called\n")
        return json.dumps({"owner": (home / "owned.txt").read_text()})


class FileMemory(MemoryProvider):
    name = "owned-file-memory"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return [SCHEMA]

    def handle_tool_call(self, name, args, **kwargs):
        return FileEngine.read(self)


def _agent():
    from run_agent import AIAgent

    definitions = [{"type": "function", "function": SCHEMA},
                   {"type": "function", "function": {**SCHEMA, "name": "execute_code"}}]
    with (patch("model_tools.get_tool_definitions", return_value=definitions),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI"),
          patch("agent.model_metadata.fetch_model_metadata", return_value={})):
        return AIAgent(api_key="test-key", base_url="https://example.invalid",
                       quiet_mode=True, skip_context_files=True, skip_memory=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "remote"])
@pytest.mark.parametrize("route", ["context", "memory", "memory-handler"])
@pytest.mark.parametrize("timing", ["policy", "invocation"])
def test_real_child_pins_inline_target_and_live_messages(tmp_path, monkeypatch, kind, route, timing):
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.memory_manager import MemoryManager
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from hermes_state import SessionDB
    from pm.shell import bash
    from tools.code_kernel import execute_in_session_kernel, shutdown_kernels_for_owner
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_remote_kernels_for_owner

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    shell = bash()
    assert shell

    class ShellTransport:
        def get_temp_dir(self):
            return str(tmp_path)

        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run([shell, "-c", command], cwd=cwd, timeout=timeout,
                env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "LANG": "C.UTF-8"},
                input=stdin_data or "", capture_output=True, text=True)
            return {"output": result.stdout, "returncode": result.returncode}

    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        (home / "owned.txt").write_text(home.name)
    task = "same-inline-parent"
    for visit, home in enumerate([homes[0], homes[1], homes[0]]):
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            agent = _agent()
            messages = [{"role": "user", "content": "owned live conversation"}]
            agent._session_messages = messages
            selected = FileEngine() if route == "context" else FileMemory()
            replacement = FileEngine() if route == "context" else FileMemory()
            selected.messages = replacement.messages = messages
            selected.effect = f"selected-{visit}.txt"
            replacement.effect = f"replacement-{visit}.txt"
            manager = MemoryManager()
            if route == "context":
                agent._context_engine_tool_names = {NAME}
                agent.context_compressor = selected
            else:
                manager.add_provider(selected)
                agent._memory_manager = manager
            db = SessionDB(db_path=home / "state.db")
            if db.get_session(task) is None:
                db.create_session(session_id=task, source="test", model="test")
            agent.session_id, agent._session_db = task, db
            change = False
            old_before, old_invoke = agent._tool_guardrails.before_call, agent._invoke_tool

            def replace():
                if route == "context":
                    agent.context_compressor = replacement
                elif route == "memory":
                    manager._tool_to_provider[NAME] = replacement
                else:
                    selected.handle_tool_call = replacement.handle_tool_call

            def before(name, args):
                decision = old_before(name, args)
                if name == NAME and change and timing == "policy":
                    replace()
                return decision

            def invoke(name, *args, **kwargs):
                if name == NAME and change and timing == "invocation":
                    replace()
                return old_invoke(name, *args, **kwargs)

            agent._tool_guardrails.before_call, agent._invoke_tool = before, invoke
            code = f"import json\nfrom hermes_tools import _call\nprint(json.dumps(_call({NAME!r}, {{}})))"

            def execute(_args):
                if kind == "local":
                    result = json.loads(execute_in_session_kernel(code, task_id=task, mode="strict",
                        child_python=sys.executable, child_cwd=str(home), sandbox_tools=frozenset({NAME}),
                        timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
                    assert result["exit_code"] == 0, result
                    return result["output"].strip()
                result = execute_in_remote_kernel(code, env=ShellTransport(), env_type="inline-fixture",
                    task_env_id=task, sandbox_tools=frozenset({NAME}), timeout=20, max_tool_calls=2,
                    reset=False, idle_exit=30)
                assert result and result["status"] == "success", result
                return result["stdout"].strip()

            def run():
                return json.loads(_run_agent_tool_execution_middleware(agent, function_name="execute_code",
                    function_args={}, effective_task_id=task, tool_call_id="parent-call", execute=execute).result)

            try:
                assert run() == {"owner": home.name}
                change = True
                rejected = run()
                assert "execution target changed" in rejected.get("error", ""), rejected
                assert (home / selected.effect).read_text() == "called\n"
                assert not (home / replacement.effect).exists()
                assert agent._session_messages is messages
                assert messages == [{"role": "user", "content": "owned live conversation"}]
                rows = db._read_all("SELECT * FROM session_tool_attempts WHERE session_id=? ORDER BY created_at", (task,))
                assert rows[-2]["state"] == "returned"
                assert rows[-1]["state"] == ("rejected" if timing == "policy" else "returned-error")
                assert (rows[-1]["dispatched_at"] is None) == (timing == "policy")
            finally:
                shutdown_kernels_for_owner(task)
                shutdown_remote_kernels_for_owner(task)
                manager.shutdown_all()
                db.close()


@pytest.mark.parametrize("route", ["context", "memory"])
def test_selected_inline_callable_cannot_be_redirected_after_resolution(tmp_path, monkeypatch, route):
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.inline_tool_executors import InlineToolContext, resolve_invoke_tool_executor
    from agent.memory_manager import MemoryManager

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    for visit, name in enumerate(["a", "b", "a"]):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        (home / "owned.txt").write_text(name)
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            selected = FileEngine() if route == "context" else FileMemory()
            replacement = FileEngine() if route == "context" else FileMemory()
            selected.effect = f"selected-{visit}.txt"
            replacement.effect = f"replacement-{visit}.txt"
            messages = [{"role": "user", "content": "live messages"}]
            selected.messages = replacement.messages = messages
            manager = MemoryManager()
            agent = SimpleNamespace(_context_engine_tool_names={NAME} if route == "context" else set(),
                                    context_compressor=selected, _memory_manager=manager)
            if route == "memory":
                manager.add_provider(selected)
            try:
                executor = resolve_invoke_tool_executor(agent, NAME)
                agent.context_compressor = replacement
                if route == "memory":
                    manager._tool_to_provider[NAME] = replacement
                    selected.handle_tool_call = replacement.handle_tool_call
                result = executor(agent, {}, InlineToolContext("task", "call", messages))
                assert json.loads(result) == {"owner": name}
                assert (home / selected.effect).read_text() == "called\n"
                assert not (home / replacement.effect).exists()
            finally:
                manager.shutdown_all()
