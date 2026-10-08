"""Owned RPC runs real agent tools once, without creating another conversation loop."""

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
def owned_sessions(tmp_path, monkeypatch):
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
    definitions = [{"type": "function", "function": copy.deepcopy(registry.get_entry(name).schema)} for name in names]
    for name in ["a", "b"]:
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("code_execution:\n  mode: strict\n  timeout: 20\n")
        with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
            with (patch("model_tools.get_tool_definitions", return_value=copy.deepcopy(definitions)),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("agent.process_bootstrap.OpenAI"),
                  patch("agent.model_metadata.fetch_model_metadata", return_value={})):
                agent = AIAgent(api_key="test-key", base_url="https://example.invalid",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
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
        session["agent"]._session_db.close()


def _call(sessions, caller, target, name, args, request, **changes):
    import tui_gateway.server as server
    from tui_gateway.tool_snapshot import session_tool_snapshot

    snapshot = session_tool_snapshot(sessions[target])
    params = {"session_id": target, "name": name, "arguments": args, "request_id": request,
              "context_id": snapshot["context_id"], "revision": snapshot["revision"], **changes}
    reply = server.dispatch({"jsonrpc": "2.0", "id": "rid", "method": "tools.call", "params": params},
                            transport=sessions[caller]["transport"])
    return reply if reply is not None else sessions[caller]["wire"].replies.get(timeout=30)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("name", ["read_file", "write_file", "todo_list", "execute_code"])
def test_owned_call_executes_once_and_replay_reads_metadata(owned_sessions, name):
    import tui_gateway.server as server

    for visit, owner in enumerate(["a", "b", "a"]):
        session = owned_sessions[owner]
        home = Path(session["profile_home"])
        source, output = home / "owned.txt", home / f"output-{visit}.txt"
        payload = f"{owner}-owned-visit-{visit}"
        source.write_text(payload)
        arguments = {"read_file": {"path": str(source)}, "write_file": {"path": str(output), "content": payload},
            "todo_list": {"todos": [{"id": "1", "content": payload, "status": "pending"}]},
            "execute_code": {"code": f"owned_rpc_marker = {payload!r}\nfrom hermes_tools import write_file\nprint(write_file({str(output)!r}, {payload!r}))"}}[name]
        messages = session["agent"]._session_messages
        reply = _call(owned_sessions, owner, owner, name, arguments, f"request-{visit}-{name}")
        assert "result" in reply, reply
        result = reply["result"]
        assert result["state"] == "returned" and result["observation"] == "handler-return", result
        assert not result["duplicate"] and result["terminal"]
        if name in {"write_file", "execute_code"}:
            assert output.read_text() == payload
            output.write_text("effect already delivered")
        if name == "execute_code":
            peek = _call(owned_sessions, owner, owner, name, {"code": "print(owned_rpc_marker)"}, f"peek-{visit}")["result"]
            assert payload in peek["output"]["output"], peek
        if name == "read_file":
            assert payload in result["output"]["content"]
        if name == "todo_list":
            assert session["agent"]._todo_store.read()[0]["content"] == payload
        replay = _call(owned_sessions, owner, owner, name, arguments, f"request-{visit}-{name}")["result"]
        assert replay["duplicate"] and replay["output"] is None and replay["observation"] == "metadata-only"
        if name in {"write_file", "execute_code"}:
            assert output.read_text() == "effect already delivered"
        conflict = _call(owned_sessions, owner, owner, name, {}, f"request-{visit}-{name}")
        assert conflict["error"]["code"] == 4092
        assert not session["running"]
        assert session["agent"]._active_session_turn_lease_holder is None
        assert session["agent"]._session_messages is messages and messages == [{"role": "user", "content": "owned conversation"}]
        token = server.bind_transport(session["transport"])
        try:
            page = server._methods["tools.attempts"]("rid", {"session_id": owner})["result"]
        finally:
            server.reset_transport(token)
        assert any(row["attempt_id"] == result["attempt_id"] for row in page["attempts"])


@pytest.mark.parametrize("failure", ["busy", "lease", "retired", "schema", "lost-result", "child-retired", "stale-context", "interrupted", "guardrail", "nonfinite", "deadline"])
def test_owned_call_rejects_lost_authority_and_preserves_unknown(owned_sessions, monkeypatch, failure):
    import tui_gateway.server as server
    from pydantic import ValidationError
    from tui_gateway.contracts.tool_call import ToolsCallParams

    session = owned_sessions["a"]
    foreign_definitions = copy.deepcopy(owned_sessions["b"]["agent"].tools)
    agent, db = session["agent"], session["agent"]._session_db
    home = Path(session["profile_home"])
    path = home / "mutation.txt"
    arguments = {"path": str(path), "content": "actual effect"}
    foreign = _call(owned_sessions, "b", "a", "write_file", arguments, "foreign")
    assert foreign["error"]["code"] == 4001 and not path.exists()
    if failure == "busy":
        session["running"] = True
    if failure == "lease":
        assert db.try_acquire_session_turn_lease(agent.session_id, "other-owner")
    if failure == "interrupted":
        agent._interrupt_requested = True
    if failure == "guardrail":
        from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
        agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=True, exact_failure_block_after=2))
        for _ in range(2):
            agent._tool_guardrails.after_call("write_file", arguments, '{"error":"earlier failure"}', failed=True)
    before = agent._tool_guardrails.before_call

    def policy(name, args):
        decision = before(name, args)
        if failure == "deadline":
            threading.Event().wait(2.1)
        if failure == "retired" or (failure == "child-retired" and name == "read_file"):
            owned_sessions["a"] = dict(session)
        if failure == "schema":
            agent.tools[0]["function"]["description"] += " changed during approval"
        return decision

    monkeypatch.setattr(agent._tool_guardrails, "before_call", policy)
    invoke = agent._invoke_tool

    def lose_result(name, *args, **kwargs):
        result = invoke(name, *args, **kwargs)
        if name == "write_file" and failure == "lost-result":
            raise RuntimeError("lost actual handler result")
        return result

    monkeypatch.setattr(agent, "_invoke_tool", lose_result)
    try:
        changes = {"revision": "0" * 64} if failure == "stale-context" else {}
        if failure == "deadline":
            changes["timeout_ms"] = 2000
        tool, requested_args = "write_file", arguments
        if failure == "nonfinite":
            requested_args = {**arguments, "content": float("nan")}
        if failure == "child-retired":
            (home / "input.txt").write_text("child owns this read")
            tool = "execute_code"
            requested_args = {"code": ("from hermes_tools import read_file, write_file\n"
                f"print(read_file({str(home / 'input.txt')!r}))\n"
                f"print(write_file({str(path)!r}, 'must not dispatch after retirement'))")}
        reply = _call(owned_sessions, "a", "a", tool, requested_args, "owned-attempt", **changes)
        if failure in {"busy", "lease"}:
            assert reply["error"]["code"] == 4009 and not path.exists()
        elif failure in {"stale-context", "interrupted"}:
            assert reply["error"]["code"] == 4092 and not path.exists()
        elif failure == "nonfinite":
            assert reply["error"]["code"] == 4000 and not path.exists()
        elif failure == "guardrail":
            assert reply["result"]["state"] == "blocked" and not path.exists()
            assert reply["result"]["observation"] == "policy-result"
        else:
            result = reply["result"]
            assert result["output"] is None and result["observation"] == "unknown"
            if failure == "lost-result":
                assert path.read_text() == "actual effect" and result["state"] == "running" and not result["terminal"]
                path.write_text("do not replay")
                replay = _call(owned_sessions, "a", "a", "write_file", arguments, "owned-attempt")["result"]
                assert replay["duplicate"] and replay["state"] == "running"
                assert path.read_text() == "do not replay"
            elif failure == "child-retired":
                assert not path.exists()
                rows = db._read_all("SELECT * FROM session_tool_attempts WHERE session_id=? AND tool_name='read_file'", (agent.session_id,))
                assert rows and all(row["state"] == "rejected" and row["dispatched_at"] is None for row in rows)
            else:
                assert not path.exists() and result["state"] == "rejected"
                row = db.get_tool_attempt(agent.session_id, result["attempt_id"])
                assert row["dispatched_at"] is None
    finally:
        session["running"] = False
        db.release_session_turn_lease(agent.session_id, "other-owner")
    assert owned_sessions["b"]["agent"].tools == foreign_definitions
    params = {"session_id": "a", "name": "read_file", "arguments": {}, "request_id": "request",
              "context_id": "context", "revision": "a" * 64}
    for invalid in ({"profile": "b"}, {"timeout_ms": True}, {"timeout_ms": 0}, {"request_id": "../other"}):
        with pytest.raises(ValidationError):
            ToolsCallParams.model_validate({**params, **invalid})
        rejected = _call(owned_sessions, "b", "b", "read_file", {}, "invalid", **invalid)
        assert rejected["error"]["code"] == 4000
