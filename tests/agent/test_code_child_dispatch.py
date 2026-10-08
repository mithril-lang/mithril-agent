"""Real code RPC consumers obey the owning agent policy and retire with their parent."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest


def _agent():
    from run_agent import AIAgent
    definitions = [{"type": "function", "function": {"name": name,
        "description": name, "parameters": {"type": "object", "properties": {}}}}
        for name in ["execute_code", "read_file"]]
    with (patch("model_tools.get_tool_definitions", return_value=definitions),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI"),
          patch("agent.model_metadata.fetch_model_metadata", return_value={})):
        return AIAgent(api_key="test-key", base_url="https://example.invalid",
            quiet_mode=True, skip_context_files=True, skip_memory=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "remote"])
def test_code_child_uses_parent_guardrail_and_retires(tmp_path, kind):
    import tools.file_tools  # noqa: F401
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
    from tools.code_execution_rpc import _default_dispatch
    from tools.code_kernel import execute_in_session_kernel, shutdown_kernels_for_owner
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_remote_kernels_for_owner
    from pm.shell import bash

    agent = _agent()
    task = f"parent-{tmp_path.name}"
    path = tmp_path / "owned.txt"
    path.write_text("owned payload")
    args = {"path": str(path), "offset": 1, "limit": 2000}
    agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, exact_failure_block_after=2))
    for _ in range(2):
        agent._tool_guardrails.after_call("read_file", args, '{"error":"earlier failure"}', failed=True)
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

    transport = ShellTransport()
    saved = []
    code = f"import json\nfrom hermes_tools import read_file\nprint(json.dumps(read_file({str(path)!r})))"

    def execute(_args):
        saved.append(_default_dispatch(task))
        if kind == "local":
            result = json.loads(execute_in_session_kernel(code, task_id=task, mode="strict",
                child_python=sys.executable, child_cwd=str(tmp_path), sandbox_tools=frozenset({"read_file"}),
                timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
            assert result["exit_code"] == 0, result
            return result["output"].strip()
        result = execute_in_remote_kernel(code, env=transport, env_type="parent-policy-fixture",
            task_env_id=task, sandbox_tools=frozenset({"read_file"}), timeout=20,
            max_tool_calls=2, reset=False, idle_exit=30)
        assert result is not None and result["status"] == "success", result
        return result["stdout"].strip()

    def run():
        return _run_agent_tool_execution_middleware(agent, function_name="execute_code",
            function_args={}, effective_task_id=task, tool_call_id="parent-call", execute=execute).result

    try:
        blocked = json.loads(run())
        assert "error" in blocked and "owned payload" not in str(blocked), blocked
        agent._tool_guardrails = ToolCallGuardrailController()
        allowed = json.loads(run())
        assert "owned payload" in allowed["content"], allowed
        retired = json.loads(saved[-1]("read_file", args))
        assert "error" in retired and "no longer active" in retired["error"], retired
    finally:
        shutdown_kernels_for_owner(task)
        shutdown_remote_kernels_for_owner(task)


def test_parent_dispatch_rechecks_profile_task_interrupt_and_current_grant(tmp_path, monkeypatch):
    import tools.file_tools  # noqa: F401
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from tools.code_execution_rpc import _default_dispatch

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        (home / "owned.txt").write_text(f"{home.name}-owned")
    agent = _agent()
    task = "same-owner"

    def scope(home):
        return server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False)

    def execute(_args):
        dispatch = _default_dispatch(task)
        args = {"path": str(homes[0] / "owned.txt")}
        assert "a-owned" in json.loads(dispatch("read_file", args))["content"]
        with scope(homes[1]):
            foreign = json.loads(dispatch("read_file", {"path": str(homes[1] / "owned.txt")}))
            assert "task/profile" in foreign["error"]
        # The real file tool deduplicates unchanged reads; change the fixture to
        # exercise a fresh read after returning to the original profile.
        (homes[0] / "owned.txt").write_text("a-owned updated")
        returned = json.loads(dispatch("read_file", args))
        assert "a-owned" in returned.get("content", ""), returned
        foreign = json.loads(_default_dispatch("foreign-task")("read_file", args))
        assert "task/profile" in foreign["error"]
        agent._interrupt_requested = True
        assert "no longer active" in json.loads(dispatch("read_file", args))["error"]
        agent._interrupt_requested = False
        agent.valid_tool_names = {"execute_code"}
        agent.enabled_toolsets = []
        assert "not available" in json.loads(dispatch("read_file", args))["error"]
        return "checked"

    with scope(homes[0]):
        managed = _run_agent_tool_execution_middleware(agent, function_name="execute_code",
            function_args={}, effective_task_id=task, tool_call_id="parent-call", execute=execute)
    assert managed.result == "checked"
