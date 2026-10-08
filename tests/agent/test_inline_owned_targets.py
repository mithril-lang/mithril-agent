"""Real inline owners remain stable while their ordinary data can be updated."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _agent(name):
    from run_agent import AIAgent

    definitions = [{"type": "function", "function": {"name": tool,
        "description": tool, "parameters": {"type": "object", "properties": {}}}}
        for tool in ["execute_code", name]]
    with (patch("model_tools.get_tool_definitions", return_value=definitions),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI"),
          patch("agent.model_metadata.fetch_model_metadata", return_value={})):
        return AIAgent(api_key="test-key", base_url="https://example.invalid",
                       quiet_mode=True, skip_context_files=True, skip_memory=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "remote"])
@pytest.mark.parametrize("name", ["todo_list", "memory", "read_terminal"])
@pytest.mark.parametrize("timing", ["policy", "invocation"])
def test_real_python_child_rejects_inline_owner_replacement(tmp_path, monkeypatch, kind, name, timing):
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from pm.shell import bash
    from tools.code_kernel import execute_in_session_kernel, shutdown_kernels_for_owner
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_remote_kernels_for_owner
    from tools.memory_tool import MemoryStore
    from tools.todo_tool import TodoStore

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
        (home / "terminal.txt").write_text(home.name)
    task = "same-inline-owner"
    attributes = {"todo_list": "_todo_store", "memory": "_memory_store", "read_terminal": "read_terminal_callback"}
    for visit, home in enumerate([homes[0], homes[1], homes[0]]):
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            agent = _agent(name)
            selected_marker, replacement_marker = home / f"selected-{visit}", home / f"replacement-{visit}"

            def callback(**kwargs):
                selected_marker.write_text(get_hermes_home().name)
                return json.dumps({"text": (get_hermes_home() / "terminal.txt").read_text()})

            def replacement_callback(**kwargs):
                replacement_marker.write_text("wrong owner")
                return json.dumps({"text": "wrong owner"})

            selected = {"todo_list": TodoStore(), "memory": MemoryStore(500, 500), "read_terminal": callback}[name]
            replacement = {"todo_list": TodoStore(), "memory": MemoryStore(500, 500),
                           "read_terminal": replacement_callback}[name]
            setattr(agent, attributes[name], selected)
            db = SessionDB(db_path=home / "state.db")
            if db.get_session(task) is None:
                db.create_session(session_id=task, source="test", model="test")
            agent.session_id, agent._session_db = task, db
            change = False
            old_before, old_invoke = agent._tool_guardrails.before_call, agent._invoke_tool

            def before(tool, args):
                decision = old_before(tool, args)
                if tool == name and change and timing == "policy":
                    setattr(agent, attributes[name], replacement)
                return decision

            def invoke(tool, *args, **kwargs):
                if tool == name and change and timing == "invocation":
                    setattr(agent, attributes[name], replacement)
                return old_invoke(tool, *args, **kwargs)

            agent._tool_guardrails.before_call, agent._invoke_tool = before, invoke

            def execute(_args):
                payload = f"{home.name}-{'replacement' if change else 'selected'}-{visit}"
                arguments = {"todo_list": {"todos": [{"id": "1", "content": payload, "status": "pending"}]},
                             "memory": {"action": "add", "target": "user", "content": payload},
                             "read_terminal": {}}[name]
                code = f"import json\nfrom hermes_tools import _call\nprint(json.dumps(_call({name!r}, {arguments!r})))"
                if kind == "local":
                    result = json.loads(execute_in_session_kernel(code, task_id=task, mode="strict",
                        child_python=sys.executable, child_cwd=str(home), sandbox_tools=frozenset({name}),
                        timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
                    assert result["exit_code"] == 0, result
                    return result["output"].strip()
                result = execute_in_remote_kernel(code, env=ShellTransport(), env_type="inline-owner-fixture",
                    task_env_id=task, sandbox_tools=frozenset({name}), timeout=20, max_tool_calls=2,
                    reset=False, idle_exit=30)
                assert result and result["status"] == "success", result
                return result["stdout"].strip()

            def run():
                return json.loads(_run_agent_tool_execution_middleware(agent, function_name="execute_code",
                    function_args={}, effective_task_id=task, tool_call_id="parent-call", execute=execute).result)

            try:
                allowed = run()
                assert "error" not in allowed, allowed
                if name == "read_terminal":
                    assert allowed["text"] == home.name and selected_marker.read_text() == home.name
                change = True
                rejected = run()
                assert "execution target changed" in rejected.get("error", ""), rejected
                assert not replacement_marker.exists()
                if name == "todo_list":
                    assert selected.read()[0]["content"] == f"{home.name}-selected-{visit}"
                    assert replacement.read() == []
                if name == "memory":
                    contents = (home / "memories" / "USER.md").read_text()
                    assert f"{home.name}-selected-{visit}" in contents
                    assert f"{home.name}-replacement-{visit}" not in contents
                rows = db._read_all("SELECT * FROM session_tool_attempts WHERE session_id=? ORDER BY created_at", (task,))
                assert rows[-2]["state"] == "returned"
                assert rows[-1]["state"] == ("rejected" if timing == "policy" else "returned-error")
                assert (rows[-1]["dispatched_at"] is None) == (timing == "policy")
            finally:
                shutdown_kernels_for_owner(task)
                shutdown_remote_kernels_for_owner(task)
                db.close()


@pytest.mark.parametrize("name", ["todo_list", "read_terminal"])
def test_selection_retains_owner_and_allows_ordinary_updates(tmp_path, monkeypatch, name):
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.inline_tool_executors import InlineToolContext, resolve_invoke_tool_executor
    from tools.todo_tool import TodoStore

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    for visit, owner in enumerate(["a", "b", "a"]):
        home = tmp_path / owner
        home.mkdir(exist_ok=True)
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            selected, replacement = TodoStore(), TodoStore()
            marker, wrong_marker = home / f"selected-{visit}", home / f"wrong-{visit}"

            def callback(**kwargs):
                marker.write_text(owner)
                return json.dumps({"text": owner})

            def replacement_callback(**kwargs):
                wrong_marker.write_text("wrong")
                return json.dumps({"text": "wrong"})

            agent = SimpleNamespace(_memory_manager=None, _todo_store=selected,
                                    read_terminal_callback=callback)
            executor = resolve_invoke_tool_executor(agent, name)
            agent._todo_store, agent.read_terminal_callback = replacement, replacement_callback
            ctx = InlineToolContext("task", "call")
            if name == "todo_list":
                for state in ["pending", "completed"]:
                    result = json.loads(executor(agent, {"todos": [{"id": "1", "content": owner, "status": state}]}, ctx))
                    assert result["todos"][0]["status"] == state
                assert selected.snapshot()["revision"] == 2
                assert replacement.read() == []
            else:
                assert json.loads(executor(agent, {}, ctx))["text"] == owner
                assert marker.read_text() == owner and not wrong_marker.exists()
