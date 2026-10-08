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
        for name in ["execute_code", "read_file", "write_file"]]
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
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    agent = _agent()
    task = f"parent-{tmp_path.name}"
    db_path = get_hermes_home() / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id=task, source="test", model="test")
    agent.session_id = task
    agent._session_db = db
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
                child_python=sys.executable, child_cwd=str(tmp_path), sandbox_tools=frozenset({"read_file", "write_file"}),
                timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
            assert result["exit_code"] == 0, result
            return result["output"].strip()
        result = execute_in_remote_kernel(code, env=transport, env_type="parent-policy-fixture",
            task_env_id=task, sandbox_tools=frozenset({"read_file", "write_file"}), timeout=20,
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
        original = agent._invoke_tool

        def lose_result(name, *call_args, **kwargs):
            result = original(name, *call_args, **kwargs)
            if name == "write_file":
                raise RuntimeError("fixture: result lost after actual write")
            return result

        agent._invoke_tool = lose_result
        code = ("import json\nfrom hermes_tools import write_file\n" +
                f"print(json.dumps(write_file({str(path)!r}, 'landed mutation')))" )
        unknown = json.loads(run())
        assert "error" in unknown and path.read_text() == "landed mutation", unknown
        rows = db._read_all("SELECT attempt_id FROM session_tool_attempts WHERE session_id=? ORDER BY created_at", (task,))
        assert len(rows) == 3
        reopened = SessionDB(db_path=db_path)
        try:
            receipts = [reopened.get_tool_attempt(task, row["attempt_id"]) for row in rows]
            assert [receipt["state"] for receipt in receipts] == ["blocked", "returned", "running"]
            assert all(receipt["terminal"] for receipt in receipts[:2])
            assert not receipts[2]["terminal"] and receipts[2]["result_digest"] is None
            assert all(receipt["parent_call_id"] == "parent-call" for receipt in receipts)
            assert all(receipt["result_digest"] and receipt["result_bytes"] > 0 for receipt in receipts[:2])
            assert "owned payload" not in json.dumps(receipts) and "landed mutation" not in json.dumps(receipts)
        finally:
            reopened.close()
    finally:
        shutdown_kernels_for_owner(task)
        shutdown_remote_kernels_for_owner(task)
        db.close()


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


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "remote"])
@pytest.mark.parametrize("publication", ["published", "deferred"])
def test_real_child_rejects_same_name_schema_change_after_policy_wait(tmp_path, monkeypatch, kind, publication):
    """A live schema replacement during policy cannot authorize a different child call."""
    import copy
    import threading
    import tools.file_tools  # noqa: F401
    import tui_gateway.server as server
    from agent import secret_scope
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from hermes_state import SessionDB
    from tools.code_kernel import execute_in_session_kernel, shutdown_kernels_for_owner
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_remote_kernels_for_owner
    from pm.shell import bash

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

    homes = {name: tmp_path / name for name in ["a", "b"]}
    for name, home in homes.items():
        home.mkdir()
        (home / "owned.txt").write_text(f"{name}-owned-payload")
        if publication == "deferred":
            (home / "config.yaml").write_text("tools:\n  tool_search:\n    defer: [read_file]\n")
    task = "same-public-parent"
    for visit, name in enumerate(["a", "b", "a"]):
        home = homes[name]
        (home / "owned.txt").write_text(f"{name}-owned-payload visit {visit}")
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            agent = _agent()
            db = SessionDB(db_path=home / "state.db")
            if db.get_session(task) is None:
                db.create_session(session_id=task, source="test", model="test")
            agent.session_id, agent._session_db = task, db
            from tools.registry import registry
            original_entry = registry.get_entry("read_file")
            if publication == "deferred":
                agent.tools = [d for d in agent.tools if d["function"]["name"] != "read_file"]
                agent.valid_tool_names.remove("read_file")
            original = copy.deepcopy(agent.tools)
            old_before = agent._tool_guardrails.before_call
            change = threading.Event()

            def before(tool_name, args):
                decision = old_before(tool_name, args)
                if tool_name == "read_file" and change.is_set():
                    if publication == "deferred":
                        schema = copy.deepcopy(original_entry.schema)
                        schema["parameters"]["required"] = ["changed-field"]
                        registry.register("read_file", original_entry.toolset, schema,
                            original_entry.handler, scope=str(home), override=True)
                        return decision
                    from tools.mcp_tool_agent import _agent_tools_lock
                    with _agent_tools_lock:
                        for definition in agent.tools:
                            if definition["function"]["name"] == "read_file":
                                definition["function"]["parameters"]["required"] = ["changed-field"]
                return decision

            agent._tool_guardrails.before_call = before
            code = ("import json\nfrom hermes_tools import read_file\n" +
                    f"print(json.dumps(read_file({str(home / 'owned.txt')!r})))")

            def execute(_args):
                if kind == "local":
                    result = json.loads(execute_in_session_kernel(code, task_id=task, mode="strict",
                        child_python=sys.executable, child_cwd=str(home), sandbox_tools=frozenset({"read_file"}),
                        timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
                    assert result["exit_code"] == 0, result
                    return result["output"].strip()
                result = execute_in_remote_kernel(code, env=ShellTransport(), env_type="schema-fixture",
                    task_env_id=task, sandbox_tools=frozenset({"read_file"}), timeout=20,
                    max_tool_calls=2, reset=False, idle_exit=30)
                assert result and result["status"] == "success", result
                return result["stdout"].strip()

            def run():
                return json.loads(_run_agent_tool_execution_middleware(agent, function_name="execute_code",
                    function_args={}, effective_task_id=task, tool_call_id="parent-call", execute=execute).result)

            try:
                allowed = run()
                assert f"{name}-owned-payload" in allowed.get("content", ""), allowed
                assert agent.tools == original  # Snapshot capture does not republish model context.
                change.set()
                rejected = run()
                assert "schema changed" in rejected.get("error", ""), rejected
                rows = db._read_all("SELECT * FROM session_tool_attempts WHERE session_id=? ORDER BY created_at", (task,))
                assert rows[-1]["state"] == "rejected" and rows[-1]["dispatched_at"] is None
                assert rows[-2]["state"] == "returned"
            finally:
                shutdown_kernels_for_owner(task)
                shutdown_remote_kernels_for_owner(task)
                if publication == "deferred":
                    registry.deregister("read_file", scope=str(home))
                db.close()
