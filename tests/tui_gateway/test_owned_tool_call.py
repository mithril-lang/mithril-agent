"""Owned RPC runs real agent tools once, without creating another conversation loop."""

import copy
import json
import queue
from pathlib import Path
import threading
from unittest.mock import patch

import pytest


def _qualification_groups(config):
    groups = [("test/browser-owned-network.test.ts",
               "^runs real JS/Python through explicit Web consent", "browser-owned-network")]
    if config["desktopMainModule"]:
        groups.append(("test/browser-owned-network.test.ts",
                       "^releases exact Desktop native intents", "native-main-owned-network"))
    if config["desktopChatSource"]:
        groups.append(("test/desktop-owned-network.test.ts", None, "desktop-owned-network"))
    # Qualify the current isolated Electron boundary before independent Web
    # work can consume its own deadline and prevent reaching this diagnostic.
    # All enabled families and final receipt/history assertions stay required.
    if config["desktopChatSource"]:
        groups.insert(0, groups.pop())
    return groups


@pytest.mark.parametrize("native,desktop", [(False, False), (True, False), (True, True)])
def test_qualification_functions_keep_independent_deadlines(tmp_path, native, desktop):
    import subprocess
    import sys

    config = {"desktopMainModule": native, "desktopChatSource": desktop}
    completed = []
    for file, title, label in _qualification_groups(config):
        # Real children model distinct bounded functions: coalescing two
        # functions spends the second function's budget on the first one.
        code = (
            "import sys,time\n"
            "title=sys.argv[1]\n"
            f"groups=1 if title or 'desktop' in sys.argv[2] or not {native!r} else 2\n"
            "for _ in range(groups):\n    time.sleep(0.8)\n    print('completed',flush=True)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", code, title or "", file],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, _ = _communicate_qualification(child, 1.4, tmp_path / (label + ".log"))
            assert child.returncode == 0
            completed.extend(stdout.splitlines())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
    assert len(completed) == 1 + native + desktop


def _communicate_qualification(process, timeout, evidence_path):
    import subprocess

    stdout, stderr = "", ""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Python exposes partial output as bytes even for a text-mode child.
        stdout = exc.output or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise
    finally:
        if evidence_path:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            payload = (stdout + "\n" + stderr).encode()
            if len(payload) > 1024 * 1024:
                marker = b"[qualification log truncated at 1 MiB]\n"
                payload = marker + payload[:1024 * 1024 - len(marker)]
            evidence_path.write_text(payload.decode("utf-8", errors="ignore"))
    assert len(stdout.encode()) + len(stderr.encode()) <= 1024 * 1024
    return stdout, stderr


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "oversize"])
def test_qualification_evidence_survives_process_outcome(tmp_path, outcome):
    import subprocess
    import sys

    # Real child output and a witness distinguish execution from a launch failure.
    witness = tmp_path / "started"
    evidence = tmp_path / "evidence" / "child.log"
    code = (
        f"from pathlib import Path; Path({str(witness)!r}).touch(); "
        "import sys,time; print('qualified stage: 読取',flush=True); "
        "print('stage diagnostic',file=sys.stderr,flush=True); "
        + ("print('x' * (1024 * 1024 + 1024),flush=True); " if outcome == "oversize" else "")
        + ("time.sleep(30)" if outcome == "timeout" else f"sys.exit({1 if outcome == 'failure' else 0})")
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        if outcome == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                _communicate_qualification(child, 2, evidence)
        elif outcome == "oversize":
            with pytest.raises(AssertionError):
                _communicate_qualification(child, 10, evidence)
            assert child.returncode == 0
        else:
            _communicate_qualification(child, 10, evidence)
            assert child.returncode == (1 if outcome == "failure" else 0)
        assert witness.exists()
        assert "qualified stage: 読取" in evidence.read_text()
        if outcome == "oversize":
            assert evidence.stat().st_size <= 1024 * 1024
            assert evidence.read_text().startswith("[qualification log truncated at 1 MiB]")
        else:
            assert "stage diagnostic" in evidence.read_text()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


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
    inline_state = getattr(request.node, "originalname", None) in {
        "test_owned_memory_preserves_profile_prompt_and_replay",
        "test_owned_session_search_uses_attached_durable_store",
        "test_owned_inline_store_replacement_retires_approval",
    }
    if inline_state:
        import tools.memory_tool  # noqa: F401
        import tools.session_search_tool  # noqa: F401
        names += ["memory", "session_search"]
    if request.node.name == "test_owned_runtime_generation_shared_by_file_terminal_and_remote_code_resolver":
        names.append("terminal")
    definitions = [{"type": "function", "function": copy.deepcopy(registry.get_entry(name).schema)} for name in names]
    collision_fixture = request.node.name == "test_real_browser_api_owned_hermes_network"
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
            with (patch("model_tools.get_tool_definitions", return_value=copy.deepcopy(profile_definitions)),
                  patch("model_tools.check_toolset_requirements", return_value={}),
                  patch("agent.process_bootstrap.OpenAI"),
                  patch("agent.model_metadata.fetch_model_metadata", return_value={})):
                agent = AIAgent(api_key="test-key", base_url="https://example.invalid",
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    enabled_toolsets=["memory"] if inline_state else None)
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


def _target_preview(sessions, caller, target, name, args, **changes):
    import tui_gateway.server as server
    from tui_gateway.tool_snapshot import session_tool_snapshot

    snapshot = session_tool_snapshot(sessions[target])
    params = {"session_id": target, "name": name, "arguments": args,
              "context_id": snapshot["context_id"], "revision": snapshot["revision"], **changes}
    reply = server.dispatch({"jsonrpc": "2.0", "id": "target", "method": "tools.target_preview", "params": params},
                            transport=sessions[caller]["transport"])
    return reply if reply is not None else sessions[caller]["wire"].replies.get(timeout=30)


def test_owned_deferred_registration_retires_approval(owned_sessions, monkeypatch):
    from tools.registry import registry
    from tools.tool_search import bridge_tool_schemas
    from tui_gateway.tool_snapshot import session_tool_snapshot
    import tui_gateway.server as server

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    session = owned_sessions["a"]
    agent = session["agent"]
    home = Path(session["profile_home"])
    name = "owned_deferred_write"
    schema = copy.deepcopy(registry.get_entry("write_file").schema)
    schema["name"] = name
    handler = registry.get_entry("write_file").handler
    probes = []

    def available():
        probes.append(True)
        return True

    def register():
        registry.register(name=name, toolset="qualification-deferred", schema=schema,
                          handler=handler, check_fn=available, scope=str(home), override=True)

    register()
    agent.enabled_toolsets = ["qualification-deferred"]
    agent.tools = bridge_tool_schemas(1, name)
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    prompt, history = copy.deepcopy(agent.tools), copy.deepcopy(agent._session_messages)
    args = lambda path: {"calls": [{"name": name, "arguments": {"path": str(path), "content": "owned"}}]}
    warm = home / "deferred-warm"
    result = _call(owned_sessions, "a", "a", "tool_call", args(warm), "deferred-warm")
    assert warm.exists(), result
    assert warm.read_text() == "owned", result
    assert result["result"]["state"] == "returned", result
    probe_count = len(probes)
    captured = session_tool_snapshot(session)
    assert len(probes) == probe_count, "owned readback must not probe deferred availability"
    foreign = session_tool_snapshot(owned_sessions["b"])
    original = registry.snapshot_registration(name, scope=str(home))
    register()  # Same schema/handler, but a replacement registration owner.
    target = home / "deferred-stale"
    result = _call(owned_sessions, "a", "a", "tool_call", args(target), "deferred-stale",
                   context_id=captured["context_id"], revision=captured["revision"])
    assert not target.exists(), result
    assert result["error"]["code"] == 4092, result
    assert agent._session_db.get_tool_attempt(agent.session_id, "rpc:deferred-stale") is None
    with registry._lock:
        registry._slot(str(home))[name] = original
    restored = session_tool_snapshot(session)
    assert restored["context_id"] != captured["context_id"]
    assert restored["revision"] == captured["revision"]
    stale = _call(owned_sessions, "a", "a", "tool_call", args(target), "deferred-restored-stale",
                  context_id=captured["context_id"], revision=captured["revision"])
    assert stale["error"]["code"] == 4092 and not target.exists(), stale
    fresh = _call(owned_sessions, "a", "a", "tool_call", args(target), "deferred-fresh")
    assert target.read_text() == "owned" and fresh["result"]["state"] == "returned", fresh
    replay = _call(owned_sessions, "a", "a", "tool_call", args(target), "deferred-fresh")
    assert replay["result"]["duplicate"] and replay["result"]["output"] is None
    assert session_tool_snapshot(owned_sessions["b"]) == foreign
    assert agent.tools == prompt and agent._session_messages == history
    assert owned_sessions["b"]["agent"]._session_db.list_tool_attempts(agent.session_id)["attempts"] == []


def test_owned_deferred_resolution_cannot_redirect_execution(owned_sessions, monkeypatch):
    import model_tools
    from tools.registry import registry
    from tools.tool_search import bridge_tool_schemas

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    session = owned_sessions["a"]
    agent = session["agent"]
    home = Path(session["profile_home"])
    entry = registry.get_entry("write_file")
    name = "owned_deferred_write"
    schema = {**copy.deepcopy(entry.schema), "name": name}

    def register():
        registry.register(name=name, toolset="qualification-deferred", schema=schema,
                          handler=entry.handler, check_fn=lambda: True, scope=str(home), override=True)

    register()
    agent.enabled_toolsets = ["qualification-deferred"]
    agent.tools = bridge_tool_schemas(1, name)
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    history = copy.deepcopy(agent._session_messages)
    resolve = model_tools._dispatch_bridge_tool
    replacements = []

    def replace_after_resolution(*args, **kwargs):
        result = resolve(*args, **kwargs)
        if args[0] == "tool_call" and result[1] is not None:
            register()
            replacements.append(True)
        return result

    monkeypatch.setattr(model_tools, "_dispatch_bridge_tool", replace_after_resolution)
    target = home / "deferred-resolution-stale"
    args = {"calls": [{"name": name, "arguments": {"path": str(target), "content": "owned"}}]}
    reply = _call(owned_sessions, "a", "a", "tool_call", args, "deferred-resolution")
    assert replacements == [True], reply
    assert not target.exists(), reply
    row = agent._session_db.get_tool_attempt(agent.session_id, "rpc:deferred-resolution")
    assert row["state"] == "returned-error", row
    replay = _call(owned_sessions, "a", "a", "tool_call", args, "deferred-resolution")
    assert replay["result"]["duplicate"] and replay["result"]["output"] is None
    assert replacements == [True]
    monkeypatch.setattr(model_tools, "_dispatch_bridge_tool", resolve)
    fresh = _call(owned_sessions, "a", "a", "tool_call", args, "deferred-resolution-fresh")
    assert target.read_text() == "owned" and fresh["result"]["state"] == "returned", fresh
    assert replacements == [True]
    assert agent._session_messages == history
    assert owned_sessions["b"]["agent"]._session_db.list_tool_attempts(agent.session_id)["attempts"] == []


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("consent", ["read-only", "approve", "retire", "queue-tool", "queue-resource",
                                    "recovery-write"])
def test_owned_deferred_real_mcp_stdio_profiles_and_replay(owned_sessions, monkeypatch, request, consent):
    import sys
    import tui_gateway.server as server
    from tools import mcp_tool_discovery as discovery
    from tools.mcp_tool_loop import _ensure_mcp_loop, _run_on_mcp_loop
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.tool_search import bridge_tool_schemas
    from tools.registry import registry

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    fixture = Path(__file__).parents[1] / "e2e/core/mcp_plugins/mcp_fixture_server.py"
    _ensure_mcp_loop()
    histories, tools, logs, names, writes, resources = {}, {}, {}, {}, {}, {}
    pids = set()
    try:
        for owner in ["a", "b"]:
            session = owned_sessions[owner]
            home = Path(session["profile_home"])
            (home / ".env").write_text(f"OWNED_MCP_CANARY=owned-{owner}\n")
            logs[owner] = home / "mcp-inbound.jsonl"
            config = {"command": sys.executable, "args": [str(fixture)], "trust": "untrusted",
                      "connect_timeout": 15, "timeout": 10,
                      "env": {"MCPE2E_LOG": str(logs[owner]), "MCPE2E_CANARY": "${OWNED_MCP_CANARY}"}}
            if consent == "recovery-write":
                config["env"]["MCPE2E_EFFECT_FILE"] = str(home / "mcp-effects.jsonl")
            with server._session_profile_runtime_scope(session):
                registered = _run_on_mcp_loop(
                    lambda: discovery._discover_and_register_server("owned-probe", config), timeout=20)
                names[owner] = next(name for name in registered if name.endswith("ro_probe"))
                writes[owner] = next(name for name in registered if name.endswith("rw_probe"))
                resources[owner] = next(name for name in registered if name.endswith("list_resources"))
                agent = session["agent"]
                agent.enabled_toolsets = [registry.get_entry(names[owner]).toolset]
                agent.tools = bridge_tool_schemas(len(registered), ", ".join(registered))
                agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
                tools[owner] = copy.deepcopy(agent.tools)
                histories[owner] = copy.deepcopy(agent._session_messages)
        for index, owner in enumerate(["a", "b", "a"]):
            args = {"calls": [{"name": names[owner], "arguments": {"nonce": str(index)}}]}
            reply = _call(owned_sessions, owner, owner, "tool_call", args, f"mcp-{index}")
            assert reply["result"]["state"] == "returned", json.dumps(reply)
            assert f"RO:owned-{owner}:{index}" in json.dumps(reply["result"]["output"]), reply
            other = "b" if owner == "a" else "a"
            foreign = _call(owned_sessions, other, owner, "tool_call", args, f"mcp-foreign-{index}")
            assert foreign["error"]["code"] == 4001, foreign
            replay = _call(owned_sessions, owner, owner, "tool_call", args, f"mcp-{index}")
            assert replay["result"]["duplicate"] and replay["result"]["output"] is None, replay
        for owner in ["a", "b"]:
            listed = _call(owned_sessions, owner, owner, "tool_call",
                           {"calls": [{"name": resources[owner], "arguments": {}}]}, "mcp-resources")
            assert listed["result"]["state"] == "returned", json.dumps(listed)
            denied = _call(owned_sessions, owner, owner, "tool_call",
                           {"calls": [{"name": writes[owner], "arguments": {"nonce": "denied"}}]},
                           "mcp-write-denied")
            assert denied["result"]["state"] == "returned-error", json.dumps(denied)
        approvals = []
        if consent in {"approve", "retire"}:
            from tools import approval_prompt

            def answer(command, description, **kwargs):
                approvals.append(command)
                if consent == "retire":
                    owned_sessions["a"]["profile_home"] = owned_sessions["b"]["profile_home"]
                return "once"

            monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", answer)
            original_home = owned_sessions["a"]["profile_home"]
            args = {"calls": [{"name": writes["a"], "arguments": {"nonce": "approved"}}]}
            try:
                approved = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-approved")
            finally:
                owned_sessions["a"]["profile_home"] = original_home
            assert len(approvals) == 1 and "rw_probe" in approvals[0], approvals
            if consent == "approve":
                assert approved["result"]["state"] == "returned", json.dumps(approved)
                assert "RW:owned-a:approved" in json.dumps(approved["result"]["output"]), approved
            else:
                received = [json.loads(line)["msg"] for line in logs["a"].read_text().splitlines()]
                assert not any(msg.get("method") == "tools/call" and msg["params"]["name"] == "rw_probe"
                               for msg in received), received
                assert approved["result"]["state"] != "returned", json.dumps(approved)
            replay = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-approved")
            assert replay["result"]["duplicate"] and replay["result"]["output"] is None, replay
            assert len(approvals) == 1
        if consent.startswith("queue-"):
            from agent.memory_provider import spawn_context_thread

            session = owned_sessions["a"]
            original_home = session["profile_home"]
            with server._session_profile_runtime_scope(session):
                connected = discovery._get_connected_server_for_call("owned-probe")
            lock = connected._rpc_lock
            waiting = threading.Event()
            result = queue.Queue()

            class ObservedLock:
                async def __aenter__(self):
                    assert lock.locked()
                    waiting.set()
                    await lock.acquire()

                async def __aexit__(self, *exc):
                    lock.release()

            async def hold():
                await lock.acquire()

            async def release():
                lock.release()

            name = names["a"] if consent == "queue-tool" else resources["a"]
            arguments = {"nonce": "queued"} if consent == "queue-tool" else {}
            args = {"calls": [{"name": name, "arguments": arguments}]}
            before = logs["a"].read_text()
            _run_on_mcp_loop(hold, timeout=5)
            connected._rpc_lock = ObservedLock()

            def invoke():
                try:
                    result.put(_call(owned_sessions, "a", "a", "tool_call", args, "mcp-queued"))
                except BaseException as exc:
                    result.put(exc)

            worker = spawn_context_thread(invoke, name="owned-mcp-queued")
            worker.start()
            try:
                assert waiting.wait(5), "owned call never reached held real RPC lock"
                session["profile_home"] = owned_sessions["b"]["profile_home"]
            finally:
                _run_on_mcp_loop(release, timeout=5)
                worker.join(15)
                connected._rpc_lock = lock
                session["profile_home"] = original_home
            assert not worker.is_alive()
            queued = result.get_nowait()
            assert not isinstance(queued, BaseException), repr(queued)
            assert queued["result"]["state"] == "returned-error", queued
            assert logs["a"].read_text() == before, "retired queued RPC crossed actual server wire"
            replay = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-queued")
            assert replay["result"]["duplicate"] and replay["result"]["output"] is None, replay
            assert logs["a"].read_text() == before
            fresh = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-queued-fresh")
            assert fresh["result"]["state"] == "returned", fresh
        if consent == "recovery-write":
            from tools import approval_prompt

            def approve_crash(command, description, **kwargs):
                approvals.append(command)
                return "once"

            monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", approve_crash)
            effect_file = Path(owned_sessions["a"]["profile_home"]) / "mcp-effects.jsonl"
            args = {"calls": [{"name": writes["a"], "arguments": {"nonce": "crash"}}]}
            crashed = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-crash-write")
            assert crashed["result"]["state"] == "returned-error", crashed
            assert '"outcome_uncertain": true' in json.dumps(crashed["result"]["output"]), crashed
            effects = effect_file.read_text()
            assert [json.loads(line)["nonce"] for line in effects.splitlines()] == ["crash"]
            before = logs["a"].read_text()
            replay = _call(owned_sessions, "a", "a", "tool_call", args, "mcp-crash-write")
            assert replay["result"]["duplicate"] and replay["result"]["output"] is None, replay
            assert logs["a"].read_text() == before and effect_file.read_text() == effects
            assert len(approvals) == 1
            recovered = _call(owned_sessions, "a", "a", "tool_call",
                              {"calls": [{"name": names["a"], "arguments": {"nonce": "recovered"}}]},
                              "mcp-recovered-read")
            assert recovered["result"]["state"] == "returned", recovered
            assert "RO:owned-a:recovered" in json.dumps(recovered["result"]["output"]), recovered
            fresh = _call(owned_sessions, "a", "a", "tool_call",
                          {"calls": [{"name": writes["a"], "arguments": {"nonce": "after-recovery"}}]},
                          "mcp-recovered-write")
            assert fresh["result"]["state"] == "returned" and len(approvals) == 2, fresh
            assert [json.loads(line)["nonce"] for line in effect_file.read_text().splitlines()] == [
                "crash", "after-recovery"]
            assert not (Path(owned_sessions["b"]["profile_home"]) / "mcp-effects.jsonl").exists()
            evidence = request.config.getoption("--owned-qualification-output")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                keys = ("state", "terminal", "duplicate", "observation", "attempt_id")
                receipt = {label: {key: reply["result"][key] for key in keys}
                           for label, reply in [("crashed", crashed), ("replay", replay),
                                                ("recoveredRead", recovered), ("freshWrite", fresh)]}
                receipt["outcomeUncertainObserved"] = True
                receipt["approvalAnswers"] = len(approvals)
                (output / "mcp-recovery-receipts.json").write_text(json.dumps(receipt, indent=2))
        for owner in ["a", "b"]:
            frames = [json.loads(line) for line in logs[owner].read_text().splitlines()]
            pids.update(frame["pid"] for frame in frames)
            messages = [frame["msg"] for frame in frames]
            assert any(msg.get("method") == "initialize" for msg in messages)
            assert any(msg.get("method") == "tools/list" for msg in messages)
            assert any(msg.get("method") == "resources/list" for msg in messages)
            assert sum(msg.get("method") == "resources/list" for msg in messages) == (
                2 if owner == "a" and consent == "queue-resource" else 1)
            calls = [msg["params"] for msg in messages if msg.get("method") == "tools/call"]
            expected = [{"name": "ro_probe", "arguments": {"nonce": nonce}}
                        for nonce in (["0", "2"] if owner == "a" else ["1"])]
            if owner == "a" and consent == "approve":
                expected.append({"name": "rw_probe", "arguments": {"nonce": "approved"}})
            if owner == "a" and consent == "queue-tool":
                expected.append({"name": "ro_probe", "arguments": {"nonce": "queued"}})
            if owner == "a" and consent == "recovery-write":
                expected.extend([{"name": "rw_probe", "arguments": {"nonce": "crash"}},
                                 {"name": "ro_probe", "arguments": {"nonce": "recovered"}},
                                 {"name": "rw_probe", "arguments": {"nonce": "after-recovery"}}])
            assert [{"name": call["name"], "arguments": call["arguments"]} for call in calls] == expected, calls
            assert owned_sessions[owner]["agent"].tools == tools[owner]
            assert owned_sessions[owner]["agent"]._session_messages == histories[owner]
            evidence = request.config.getoption("--owned-qualification-output")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                (output / f"mcp-{consent}-{owner}-inbound.jsonl").write_text(logs[owner].read_text())
                effects = Path(owned_sessions[owner]["profile_home"]) / "mcp-effects.jsonl"
                if effects.exists():
                    (output / f"mcp-{consent}-{owner}-effects.jsonl").write_text(effects.read_text())
    finally:
        for session in owned_sessions.values():
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                shutdown_mcp_servers(scope=session["profile_home"], names={"owned-probe"})
    import psutil
    assert len(pids) == (3 if consent == "recovery-write" else 2), pids
    assert all(not psutil.pid_exists(pid) for pid in pids), pids


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["read-only", "write-capable"])
def test_owned_real_mcp_http_session_expiry(owned_sessions, monkeypatch, request, kind):
    import subprocess
    import sys
    import time
    import tui_gateway.server as server
    from tools import approval_prompt, mcp_tool_discovery as discovery
    from tools.environments.local import served_profile_child_env
    from tools.mcp_tool_loop import _ensure_mcp_loop, _run_on_mcp_loop
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.tool_search import bridge_tool_schemas
    from tools.registry import registry

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    fixture = Path(__file__).parents[1] / "e2e/core/mcp_plugins/mcp_fixture_server.py"
    _ensure_mcp_loop()
    processes, logs, effects, names, histories, schemas = {}, {}, {}, {}, {}, {}
    approvals = []

    def approve(command, description, **kwargs):
        approvals.append(command)
        return "once"

    monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", approve)
    try:
        for owner in ["a", "b"]:
            session = owned_sessions[owner]
            home = Path(session["profile_home"])
            logs[owner], effects[owner] = home / "http-inbound.jsonl", home / "http-effects.jsonl"
            port_file, fault = home / "http-port", home / "404-budget"
            fault.write_text("1" if owner == "a" else "0")
            with server._session_profile_runtime_scope(session):
                env = served_profile_child_env(base={}, target_home=home)
                env.update({"MCPE2E_TRANSPORT": "http", "MCPE2E_LOG": str(logs[owner]),
                            "MCPE2E_PORT_FILE": str(port_file), "MCPE2E_404_CALLS": str(fault),
                            "MCPE2E_EFFECT_FILE": str(effects[owner]), "MCPE2E_CANARY": f"owned-{owner}"})
                with (home / "http-server.stderr").open("ab") as stderr:
                    processes[owner] = subprocess.Popen([sys.executable, str(fixture)], env=env,
                                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                       stderr=stderr)
                deadline = time.monotonic() + 15
                while not port_file.exists() and processes[owner].poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert port_file.exists(), "fixture HTTP server did not become ready"
                port, pid = port_file.read_text().split()
                assert int(pid) == processes[owner].pid
                config = {"url": f"http://127.0.0.1:{port}/mcp", "trust": "untrusted",
                          "connect_timeout": 15, "timeout": 10}
                registered = _run_on_mcp_loop(
                    lambda: discovery._discover_and_register_server("owned-http", config), timeout=20)
                names[owner] = {n: next(name for name in registered if name.endswith(n))
                                for n in ["ro_probe", "rw_probe"]}
                agent = session["agent"]
                agent.enabled_toolsets = [registry.get_entry(names[owner]["ro_probe"]).toolset]
                agent.tools = bridge_tool_schemas(len(registered), ", ".join(registered))
                agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
                schemas[owner], histories[owner] = copy.deepcopy(agent.tools), copy.deepcopy(agent._session_messages)
        for index, owner in enumerate(["a", "b", "a"]):
            reply = _call(owned_sessions, owner, owner, "tool_call",
                          {"calls": [{"name": names[owner]["ro_probe"], "arguments": {"nonce": str(index)}}]},
                          f"http-warm-{index}")
            assert reply["result"]["state"] == "returned", reply
            assert f"RO:owned-{owner}:{index}" in json.dumps(reply["result"]["output"])
        tool = "ro_probe" if kind == "read-only" else "rw_probe"
        args = {"calls": [{"name": names["a"][tool], "arguments": {"nonce": "expired"}}]}
        expired = _call(owned_sessions, "a", "a", "tool_call", args, "http-expired")
        if kind == "read-only":
            assert expired["result"]["state"] == "returned", expired
            assert "RO:owned-a:expired" in json.dumps(expired["result"]["output"]), expired
            assert not effects["a"].exists()
        else:
            assert expired["result"]["state"] == "returned-error", expired
            assert '"outcome_uncertain": true' in json.dumps(expired["result"]["output"]), expired
            assert [json.loads(line)["nonce"] for line in effects["a"].read_text().splitlines()] == ["expired"]
        before = logs["a"].read_text()
        foreign = _call(owned_sessions, "b", "a", "tool_call", args, "http-foreign")
        assert foreign["error"]["code"] == 4001, foreign
        assert logs["a"].read_text() == before
        replay = _call(owned_sessions, "a", "a", "tool_call", args, "http-expired")
        assert replay["result"]["duplicate"] and replay["result"]["output"] is None, replay
        assert logs["a"].read_text() == before
        fresh = _call(owned_sessions, "a", "a", "tool_call",
                      {"calls": [{"name": names["a"]["ro_probe"], "arguments": {"nonce": "fresh"}}]},
                      "http-fresh")
        assert fresh["result"]["state"] == "returned", fresh
        assert "RO:owned-a:fresh" in json.dumps(fresh["result"]["output"])
        assert len(approvals) == (1 if kind == "write-capable" else 0)
        evidence = request.config.getoption("--owned-qualification-output")
        if evidence:
            output = Path(evidence)
            output.mkdir(parents=True, exist_ok=True)
            keys = ("state", "terminal", "duplicate", "observation", "attempt_id")
            receipts = {label: {key: reply["result"][key] for key in keys}
                        for label, reply in [("expired", expired), ("replay", replay), ("fresh", fresh)]}
            receipts["writeOutcomeUncertainObserved"] = kind == "write-capable"
            receipts["foreignTransportRejected"] = True
            (output / f"http-{kind}-receipts.json").write_text(json.dumps(receipts, indent=2))
        for owner in ["a", "b"]:
            messages = [json.loads(line)["msg"] for line in logs[owner].read_text().splitlines()]
            assert sum(msg.get("method") == "initialize" for msg in messages) == (2 if owner == "a" else 1)
            calls = [msg["params"] for msg in messages if msg.get("method") == "tools/call"]
            expired_calls = [c for c in calls if c["arguments"].get("nonce") == "expired"]
            assert len(expired_calls) == (0 if owner == "b" else 2 if kind == "read-only" else 1)
            if owner == "a":
                marker = "injected_404_before_dispatch" if kind == "read-only" else "injected_404_after_effect"
                assert sum(marker in msg for msg in messages) == 1
            assert owned_sessions[owner]["agent"].tools == schemas[owner]
            assert owned_sessions[owner]["agent"]._session_messages == histories[owner]
            assert not effects["b"].exists()
            evidence = request.config.getoption("--owned-qualification-output")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                (output / f"http-{kind}-{owner}-inbound.jsonl").write_text(logs[owner].read_text())
                stderr = Path(owned_sessions[owner]["profile_home"]) / "http-server.stderr"
                (output / f"http-{kind}-{owner}-server.stderr").write_text(stderr.read_text())
                if effects[owner].exists():
                    (output / f"http-{kind}-{owner}-effects.jsonl").write_text(effects[owner].read_text())
    finally:
        for session in owned_sessions.values():
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                shutdown_mcp_servers(scope=session["profile_home"], names={"owned-http"})
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    import psutil
    assert len(processes) == 2 and all(not psutil.pid_exists(p.pid) for p in processes.values())


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_owned_target_preview_pins_exact_request_and_path(owned_sessions, monkeypatch, tool):
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    for visit, owner in enumerate(["a", "b", "a"]):
        home = Path(owned_sessions[owner]["profile_home"])
        original, redirected = home / f"preview-original-{visit}", home / f"preview-redirected-{visit}"
        original.mkdir()
        redirected.mkdir()
        alias = home / f"preview-alias-{visit}"
        alias.symlink_to(original, target_is_directory=True)
        if tool == "read_file":
            (original / "note.txt").write_text(f"original owner {owner} visit {visit}")
            (redirected / "note.txt").write_text(f"redirected owner {owner} visit {visit}")
        before = tuple(path.read_text() if path.exists() else None
                       for path in [original / "note.txt", redirected / "note.txt"])
        args = {"path": str(alias / "note.txt")}
        if tool == "write_file":
            args["content"] = "approved content"
        preview = _target_preview(owned_sessions, owner, owner, tool, args)["result"]
        binding = preview["target_binding"]
        assert binding["coverage"] == "partial" and binding["target"]["path"] == str(original / "note.txt")
        assert tuple(path.read_text() if path.exists() else None
                     for path in [original / "note.txt", redirected / "note.txt"]) == before
        changed_args = {**args, **({"content": "other"} if tool == "write_file" else {"limit": 2})}
        changed = _call(owned_sessions, owner, owner, tool, changed_args,
                        f"different-{visit}", target_digest=binding["digest"])
        assert changed["error"]["code"] == 4092
        alias.unlink()
        alias.symlink_to(redirected, target_is_directory=True)
        stale = _call(owned_sessions, owner, owner, tool, args, f"stale-preview-{visit}",
                      target_digest=binding["digest"])
        assert stale["error"]["code"] == 4092
        assert tuple(path.read_text() if path.exists() else None
                     for path in [original / "note.txt", redirected / "note.txt"]) == before
        fresh = _target_preview(owned_sessions, owner, owner, tool, args)["result"]["target_binding"]
        assert fresh["digest"] != binding["digest"]

        def redirect(*, args, next_call, **kwargs):
            alias.unlink()
            alias.symlink_to(original, target_is_directory=True)
            return next_call(args)

        manager._middleware["tool_execution"] = [redirect]
        mid = _call(owned_sessions, owner, owner, tool, args, f"mid-preview-{visit}",
                    target_digest=fresh["digest"])["result"]
        assert mid["state"] == "rejected" and mid["output"] is None
        row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", mid["attempt_id"])
        assert row["dispatched_at"] is None
        assert tuple(path.read_text() if path.exists() else None
                     for path in [original / "note.txt", redirected / "note.txt"]) == before
        manager._middleware.clear()
        alias.unlink()
        alias.symlink_to(redirected, target_is_directory=True)
        result = _call(owned_sessions, owner, owner, tool, args, f"fresh-preview-{visit}",
                       target_digest=fresh["digest"])["result"]
        assert result["state"] == "returned"
        if tool == "read_file":
            assert before[1] in result["output"]["content"]
            assert before[0] not in result["output"]["content"]
            assert (original / "note.txt").read_text() == before[0]
        else:
            assert (redirected / "note.txt").read_text() == "approved content"
            assert not (original / "note.txt").exists()
        replay = _call(owned_sessions, owner, owner, tool, args, f"fresh-preview-{visit}",
                       target_digest=fresh["digest"])["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
        conflict = _call(owned_sessions, owner, owner, tool, args, f"fresh-preview-{visit}",
                         target_digest=binding["digest"])
        assert conflict["error"]["code"] == 4092


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("target", ["content", "files"])
@pytest.mark.parametrize("output_mode", ["content", "files_only", "count"])
def test_owned_search_root_target_and_replay(owned_sessions, monkeypatch, target, output_mode):
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    for visit, owner in enumerate(["a", "b", "a"]):
        home = Path(owned_sessions[owner]["profile_home"])
        old, fresh, alias = home / f"old-{visit}", home / f"fresh-{visit}", home / f"search-{visit}"
        old.mkdir()
        fresh.mkdir()
        marker = f"search-canary-{owner}-{visit}"
        (old / "old.txt").write_text(marker)
        (fresh / "fresh.txt").write_text(marker)
        alias.symlink_to(old, target_is_directory=True)
        args = {"pattern": marker if target == "content" else "*.txt", "target": target,
                "path": str(alias), "output_mode": output_mode}
        first = _target_preview(owned_sessions, owner, owner, "search_files", args)["result"]["target_binding"]
        assert first is not None and first["target"]["path"] == str(old.resolve()), first
        alias.unlink()
        alias.symlink_to(fresh, target_is_directory=True)
        binding = _target_preview(owned_sessions, owner, owner, "search_files", args)["result"]["target_binding"]
        assert binding["target"]["path"] == str(fresh.resolve()) and binding["digest"] != first["digest"]
        stale = _call(owned_sessions, owner, owner, "search_files", args, f"search-stale-{visit}",
                      target_digest=first["digest"])
        assert stale["error"]["code"] == 4092, stale
        changed = _call(owned_sessions, owner, owner, "search_files", {**args, "limit": 2},
                        f"search-changed-{visit}", target_digest=binding["digest"])
        assert changed["error"]["code"] == 4092, changed

        def redirect(*, args, next_call, **kwargs):
            alias.unlink()
            alias.symlink_to(old, target_is_directory=True)
            return next_call(args)

        manager._middleware["tool_execution"] = [redirect]
        rejected = _call(owned_sessions, owner, owner, "search_files", args, f"search-mid-{visit}",
                         target_digest=binding["digest"])["result"]
        assert rejected["state"] == "rejected" and rejected["output"] is None, rejected
        row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", rejected["attempt_id"])
        assert row["dispatched_at"] is None
        manager._middleware.clear()
        alias.unlink()
        alias.symlink_to(fresh, target_is_directory=True)
        result = _call(owned_sessions, owner, owner, "search_files", args, f"search-fresh-{visit}",
                       target_digest=binding["digest"])["result"]
        assert result["state"] == "returned", result
        serialized = json.dumps(result["output"])
        assert "fresh.txt" in serialized and "old.txt" not in serialized, result
        replay = _call(owned_sessions, owner, owner, "search_files", args, f"search-fresh-{visit}",
                       target_digest=binding["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None
        assert (old / "old.txt").read_text() == (fresh / "fresh.txt").read_text() == marker


@pytest.mark.parametrize("path_args", [{}, {"path": None}, {"path": ""}, {"path": "  "}, {"path": "."}])
@pytest.mark.parametrize("output_mode", ["content", "files_only", "count"])
def test_owned_search_defaults_stay_in_selected_profile(owned_sessions, path_args, output_mode):
    frozen = {owner: (copy.deepcopy(session["agent"].tools), copy.deepcopy(session["history"]))
              for owner, session in owned_sessions.items()}
    marker = f"selected-search-canary-{Path(owned_sessions['a']['profile_home']).parent.name}"
    for owner, session in owned_sessions.items():
        (Path(session["profile_home"]) / f"selected-{owner}.txt").write_text(marker)
    for visit, owner in enumerate(["a", "b", "a"]):
        args = {"pattern": marker, "output_mode": output_mode, **path_args}
        foreign = "b" if owner == "a" else "a"
        denied = _target_preview(owned_sessions, foreign, owner, "search_files", args)
        assert denied["error"]["code"] == 4001
        binding = _target_preview(owned_sessions, owner, owner, "search_files", args)["result"]["target_binding"]
        assert binding["target"]["path"] == str(Path(owned_sessions[owner]["profile_home"]).resolve())
        denied = _call(owned_sessions, foreign, owner, "search_files", args, f"default-foreign-{visit}",
                       target_digest=binding["digest"])
        assert denied["error"]["code"] == 4001
        result = _call(owned_sessions, owner, owner, "search_files", args, f"default-{visit}",
                       target_digest=binding["digest"])["result"]
        assert result["state"] == "returned", result
        serialized = json.dumps(result["output"])
        assert f"selected-{owner}.txt" in serialized and f"selected-{foreign}.txt" not in serialized, result
        replay = _call(owned_sessions, owner, owner, "search_files", args, f"default-{visit}",
                       target_digest=binding["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None
    for owner, session in owned_sessions.items():
        assert (session["agent"].tools, session["history"]) == frozen[owner]


def test_owned_target_preview_respects_owner_and_unknown_tools(owned_sessions):
    for owner, foreign in [("a", "b"), ("b", "a"), ("a", "b")]:
        args = {"path": str(Path(owned_sessions[owner]["profile_home"]) / "owner.txt"), "content": "x"}
        denied = _target_preview(owned_sessions, foreign, owner, "write_file", args)
        assert denied["error"]["code"] == 4001
        unknown = _target_preview(owned_sessions, owner, owner, "todo_list", {})["result"]
        assert unknown["target_binding"] is None
        unfrozen = _target_preview(owned_sessions, owner, owner, "terminal", {})
        assert unfrozen["error"]["code"] == 4092
        stale = _target_preview(owned_sessions, owner, owner, "write_file", args, context_id="0" * 32)
        assert stale["error"]["code"] == 4092
        call = _call(owned_sessions, owner, owner, "todo_list", {}, f"unknown-{owner}", target_digest="0" * 64)
        assert call["error"]["code"] == 4092
        assert not Path(args["path"]).exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stage", ["tool_request", "tool_execution", "pre_tool_call"])
@pytest.mark.parametrize("tool", ["write_file", "execute_code"])
def test_owned_middleware_cannot_change_exact_intent(owned_sessions, monkeypatch, stage, tool):
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    for visit, owner in enumerate(["a", "b", "a"]):
        home = Path(owned_sessions[owner]["profile_home"])
        approved, changed = home / f"approved-{visit}.txt", home / f"changed-{visit}.txt"
        arguments = {"path": str(approved), "content": "approved payload"} if tool == "write_file" else {
            "code": f"from hermes_tools import write_file\nprint(write_file({str(approved)!r}, 'approved payload'))"}

        def rewrite(*, tool_name, args, next_call=None, **kwargs):
            # In-place changes must also be fenced, including nested code children.
            if tool_name == "write_file":
                args.update(path=str(changed), content="unapproved payload")
            if stage == "pre_tool_call":
                return {"action": "modify", "args": args}
            return {"args": args} if stage == "tool_request" else next_call(args)

        if stage == "pre_tool_call":
            manager._hooks[stage] = [rewrite]
        else:
            manager._middleware[stage] = [rewrite]
        request = f"rewrite-{stage}-{tool}-{visit}"
        result = _call(owned_sessions, owner, owner, tool, arguments, request)["result"]
        assert not changed.exists() and not approved.exists(), result
        if tool == "write_file":
            assert result["state"] == "rejected" and result["observation"] == "policy-result", result
            receipt = owned_sessions[owner]["agent"]._session_db.get_tool_attempt(
                "same-durable-owner", result["attempt_id"])
            assert receipt["dispatched_at"] is None
        else:
            assert "intent" in result["output"]["output"], result
        replay = _call(owned_sessions, owner, owner, tool, arguments, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only" and replay["output"] is None
        assert not changed.exists() and not approved.exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stage", ["tool_request", "tool_execution", "pre_tool_call"])
@pytest.mark.parametrize("tool", ["write_file", "execute_code"])
def test_owned_symlink_target_cannot_change_after_admission(owned_sessions, monkeypatch, stage, tool):
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    for visit, owner in enumerate(["a", "b", "a"]):
        home = Path(owned_sessions[owner]["profile_home"])
        original, redirected = home / f"original-{visit}", home / f"redirected-{visit}"
        original.mkdir()
        redirected.mkdir()
        alias = home / f"alias-{visit}"
        alias.symlink_to(original, target_is_directory=True)
        path = alias / "note.txt"

        def redirect(*, tool_name, args, next_call=None, **kwargs):
            if tool_name == "write_file":
                alias.unlink()
                alias.symlink_to(redirected, target_is_directory=True)
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        if stage == "pre_tool_call":
            manager._hooks[stage] = [redirect]
        else:
            manager._middleware[stage] = [redirect]
        args = {"path": str(path), "content": "exact unchanged JSON"} if tool == "write_file" else {
            "code": f"from hermes_tools import write_file\nprint(write_file({str(path)!r}, 'exact unchanged JSON'))"}
        request = f"target-{stage}-{tool}-{visit}"
        result = _call(owned_sessions, owner, owner, tool, args, request)["result"]
        assert not (redirected / "note.txt").exists() and not (original / "note.txt").exists(), result
        if tool == "write_file":
            assert result["state"] == "rejected" and result["observation"] == "policy-result", result
            row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
            assert row["dispatched_at"] is None
        else:
            assert "target" in result["output"]["output"], result
        replay = _call(owned_sessions, owner, owner, tool, args, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only"
        manager._hooks.clear()
        manager._middleware.clear()
        alias.unlink()
        alias.symlink_to(original, target_is_directory=True)
        fresh = _call(owned_sessions, owner, owner, "write_file", {"path": str(path), "content": "fresh"}, f"fresh-{request}")["result"]
        assert fresh["state"] == "returned" and (original / "note.txt").read_text() == "fresh"
        assert not (redirected / "note.txt").exists()


@pytest.mark.platforms("posix")
def test_owned_unresolvable_target_is_recorded_before_dispatch(owned_sessions):
    for visit, owner in enumerate(["a", "b", "a"]):
        home = Path(owned_sessions[owner]["profile_home"])
        args = {"path": str(home) + "/\0note.txt", "content": "never write"}
        request = f"unresolvable-{visit}"
        result = _call(owned_sessions, owner, owner, "write_file", args, request)["result"]
        assert result["state"] == "rejected" and result["observation"] == "policy-result", result
        row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
        assert row["dispatched_at"] is None
        replay = _call(owned_sessions, owner, owner, "write_file", args, request)["result"]
        assert replay["duplicate"] and replay["observation"] == "metadata-only"


@pytest.mark.platforms("posix")
def test_owned_middleware_equivalent_json_still_executes_once(owned_sessions, monkeypatch):
    import tui_gateway.server as server
    from agent.code_child_dispatch import bind_code_child_dispatch, resolve_code_child_dispatch
    from agent.tool_executor import _ToolCallRef
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    manager._middleware["tool_execution"] = [
        lambda *, args, next_call, **kwargs: next_call(dict(reversed(list(args.items()))))]
    home = Path(owned_sessions["a"]["profile_home"])
    path = home / "equivalent.txt"
    arguments = {"path": str(path), "content": "same exact intent"}
    result = _call(owned_sessions, "a", "a", "write_file", arguments, "equivalent")["result"]
    assert result["state"] == "returned" and path.read_text() == "same exact intent", result
    path.write_text("already delivered")
    replay = _call(owned_sessions, "a", "a", "write_file", arguments, "equivalent")["result"]
    assert replay["duplicate"] and replay["output"] is None
    assert path.read_text() == "already delivered"

    # Ordinary model-origin code children keep the existing rewrite feature.
    model_path = home / "model-rewritten.txt"
    manager._middleware["tool_execution"] = [lambda *, args, next_call, **kwargs:
        next_call({**args, "path": str(model_path), "content": "model middleware payload"})]
    session = owned_sessions["a"]
    parent = _ToolCallRef("execute_code", {}, "model-task", "model-parent", [])
    with server._session_profile_runtime_scope(session, hydrate_secrets=False):
        with bind_code_child_dispatch(session["agent"], parent):
            output = json.loads(resolve_code_child_dispatch("model-task")("write_file", arguments))
    assert "error" not in output, output
    assert model_path.read_text() == "model middleware payload"
    assert path.read_text() == "already delivered"


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


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("replacement", ["entry", "in-place"])
def test_owned_equal_descriptor_handler_replacement_retires_approval(owned_sessions, monkeypatch, replacement):
    import tui_gateway.server as server
    from tools.registry import registry
    from tui_gateway.tool_snapshot import session_tool_snapshot

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    a, b = owned_sessions["a"], owned_sessions["b"]
    home = Path(a["profile_home"])
    with server._session_profile_runtime_scope(a, hydrate_secrets=False):
        entry = registry.get_entry("write_file")
        registry.register("write_file", entry.toolset, entry.schema, entry.handler,
                          scope=str(home), effect_manifest=entry.effect_manifest)
        original_entry = registry.get_entry("write_file")
        handler = original_entry.handler
    original, foreign = session_tool_snapshot(a), session_tool_snapshot(b)
    calls = []

    def newer(args, **kwargs):
        calls.append("newer")
        return handler(args, **kwargs)

    with server._session_profile_runtime_scope(a, hydrate_secrets=False):
        if replacement == "entry":
            registry.register("write_file", original_entry.toolset, original_entry.schema, newer,
                              scope=str(home), effect_manifest=original_entry.effect_manifest)
        else:
            original_entry.handler = newer
    path = home / "changed-handler.txt"
    arguments = {"path": str(path), "content": "owned effect"}
    stale = _call(owned_sessions, "a", "a", "write_file", arguments, "stale-handler",
                  context_id=original["context_id"], revision=original["revision"])
    assert stale.get("error", {}).get("code") == 4092 and not path.exists() and not calls, stale
    changed = session_tool_snapshot(a)
    assert changed["revision"] == original["revision"] and changed["context_id"] != original["context_id"]
    assert changed["definitions"] == original["definitions"] and changed["effect_manifests"] == original["effect_manifests"]
    assert session_tool_snapshot(b) == foreign
    fresh = _call(owned_sessions, "a", "a", "write_file", arguments, "fresh-handler")["result"]
    assert fresh["state"] == "returned" and path.read_text() == "owned effect" and calls == ["newer"]
    with server._session_profile_runtime_scope(a, hydrate_secrets=False):
        registry.get_entry("write_file").handler = handler
    restored = session_tool_snapshot(a)
    assert restored["revision"] == original["revision"] and restored["context_id"] != original["context_id"]
    path.unlink()
    refused = _call(owned_sessions, "a", "a", "write_file", arguments, "restored-old-handler",
                    context_id=original["context_id"], revision=original["revision"])
    assert refused.get("error", {}).get("code") == 4092 and not path.exists()
    assert session_tool_snapshot(b) == foreign


@pytest.mark.platforms("posix")
def test_owned_effect_manifest_retires_profile_context_without_changing_prompt(owned_sessions, monkeypatch):
    import tui_gateway.server as server
    from tools.registry import registry
    from tui_gateway.tool_snapshot import session_tool_snapshot

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    a, b = owned_sessions["a"], owned_sessions["b"]
    original = session_tool_snapshot(a)
    foreign = session_tool_snapshot(b)
    declarations = {m["name"]: m for m in original["effect_manifests"]}
    assert declarations["write_file"]["coverage"] == "partial"
    assert declarations["todo_list"]["coverage"] == "unknown"
    prompt = copy.deepcopy(a["agent"].tools)
    home = Path(a["profile_home"])
    path = home / "manifest-write.txt"
    with server._session_profile_runtime_scope(a, hydrate_secrets=False):
        entry = registry.get_entry("write_file")
        metadata = copy.deepcopy(entry.effect_manifest)
        metadata["effects"].append("file.metadata")
        registry.register("write_file", entry.toolset, entry.schema, entry.handler,
                          check_fn=entry.check_fn, scope=str(home), effect_manifest=metadata)
        for malformed in ({**metadata, "coverage": "complete"},
                          {**metadata, "effects": []}, {**metadata, "effects": [float("nan")]}):
            with pytest.raises(ValueError):
                registry.register("write_file", entry.toolset, entry.schema, entry.handler,
                                  scope=str(home), effect_manifest=malformed)
        metadata["effects"].append("caller-owned-mutation")
    changed = session_tool_snapshot(a)
    assert changed["revision"] != original["revision"] and changed["context_id"] != original["context_id"]
    assert all("caller-owned-mutation" not in m["effects"] for m in changed["effect_manifests"])
    assert session_tool_snapshot(b) == foreign
    assert a["agent"].tools == prompt
    rejected = _call(owned_sessions, "a", "a", "write_file", {"path": str(path), "content": "stale"},
                     "old-manifest", context_id=original["context_id"], revision=original["revision"])
    assert rejected["error"]["code"] == 4092 and not path.exists()
    with server._session_profile_runtime_scope(a, hydrate_secrets=False):
        entry = registry.get_entry("write_file")
        entry.effect_manifest = copy.deepcopy(declarations["write_file"])
        entry.effect_manifest.pop("name")
    restored = session_tool_snapshot(a)
    assert restored["revision"] == original["revision"] and restored["context_id"] != original["context_id"]
    result = _call(owned_sessions, "a", "a", "write_file", {"path": str(path), "content": "fresh"},
                   "fresh-manifest")["result"]
    assert result["state"] == "returned" and path.read_text() == "fresh"
    assert session_tool_snapshot(b) == foreign and a["agent"].tools == prompt


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("timing", ["policy", "handler-selection"])
def test_owned_effect_manifest_changes_during_policy_never_dispatch(owned_sessions, monkeypatch, timing):
    from tools.registry import registry

    monkeypatch.setattr(registry, "_scoped_tools", copy.deepcopy(registry._scoped_tools))
    monkeypatch.setattr(registry, "_generation", registry._generation)
    agent = owned_sessions["a"]["agent"]
    before = agent._tool_guardrails.before_call

    def change(name, args):
        decision = before(name, args)
        if timing == "policy":
            registry.get_entry(name).effect_manifest["effects"].append("file.metadata")
        return decision

    # Keep the mutation on a profile overlay, never the shared built-in entry.
    import tui_gateway.server as server
    session = owned_sessions["a"]
    with server._session_profile_runtime_scope(session, hydrate_secrets=False):
        entry = registry.get_entry("write_file")
        registry.register("write_file", entry.toolset, entry.schema, entry.handler,
                          scope=session["profile_home"], effect_manifest=entry.effect_manifest)
    monkeypatch.setattr(agent._tool_guardrails, "before_call", change)
    invoke = agent._invoke_tool

    def change_at_selection(name, *args, **kwargs):
        if timing == "handler-selection":
            registry.get_entry(name).effect_manifest["effects"].append("file.metadata")
        return invoke(name, *args, **kwargs)

    monkeypatch.setattr(agent, "_invoke_tool", change_at_selection)
    path = Path(session["profile_home"]) / "mid-policy.txt"
    result = _call(owned_sessions, "a", "a", "write_file", {"path": str(path), "content": "refused"},
                   "mid-manifest")["result"]
    row = agent._session_db.get_tool_attempt(agent.session_id, result["attempt_id"])
    expected = "rejected" if timing == "policy" else "returned-error"
    assert row["state"] == expected and not path.exists(), result
    assert (row["dispatched_at"] is None) == (timing == "policy")
    replay = _call(owned_sessions, "a", "a", "write_file", {"path": str(path), "content": "refused"},
                   "mid-manifest")["result"]
    assert replay["duplicate"] and replay["observation"] == "metadata-only" and not path.exists()


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


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", ["roundtrip", "lost-result"])
def test_compiled_owned_sdk_real_stdio_roundtrip(owned_sessions, monkeypatch, mode, request):
    """Real Node SDK pipes reach the real dispatcher/agent/DB, not JSON reply fixtures.

    Opt-in cross-repository qualifier: --owned-sdk-module identifies the
    already-built SDK module. It is not a runtime configuration or provider key.
    """
    import shutil
    import subprocess
    import tui_gateway.server as server
    from tui_gateway.transport import StdioTransport

    module = request.config.getoption("--owned-sdk-module")
    node = shutil.which("node")
    if not module or not node:
        pytest.skip("cross-repository qualifier needs a compiled owned SDK module and Node")
    assert Path(module).is_file(), "compiled SDK module missing"
    adapter = request.config.getoption("--owned-dashboard-adapter")
    if adapter:
        assert Path(adapter).is_file(), "compiled Desktop adapter missing"
    roots = {key: value["profile_home"] for key, value in owned_sessions.items()}
    for home in roots.values():
        for visit in range(3):
            for prefix in ["owned", "child"]:
                (Path(home) / f"{prefix}-{visit}.txt").write_text(f"{Path(home).name}-owned")
    if mode == "lost-result":
        agent = owned_sessions["a"]["agent"]
        original = agent._invoke_tool

        def lose_after_effect(name, *args, **kwargs):
            result = original(name, *args, **kwargs)
            if name == "write_file":
                raise RuntimeError("qualification lost handler result after actual write")
            return result

        monkeypatch.setattr(agent, "_invoke_tool", lose_after_effect)
    process = subprocess.Popen(
        [node, str(Path(__file__).parent / "fixtures" / "owned_sdk_stdio.mjs"),
         str(Path(module).resolve()), mode, json.dumps(roots), adapter or ""],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    lock = threading.Lock()
    transports = {
        key: StdioTransport(lambda: process.stdin, lock) for key in owned_sessions
    }
    for key, session in owned_sessions.items():
        session["transport"] = transports[key]
    report, failures, methods = [], [], []
    previews = {}

    def read_requests():
        active = "a"
        try:
            for line in process.stdout:
                request = json.loads(line)
                method, params = request["method"], request["params"]
                if method == "fixture.select":
                    assert params["owner"] in owned_sessions
                    active = params["owner"]
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                elif method == "fixture.mark":
                    path = Path(params["path"])
                    assert path.parent == Path(roots[active])
                    assert path.read_text() == (
                        "effect-before-loss" if mode == "lost-result"
                        else f"{active}-effect-{path.stem.split('-')[-1]}"
                    )
                    path.write_text("effect-already-delivered")
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                elif method == "fixture.report":
                    report.append(params)
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                else:
                    assert method in {"tools.show", "tools.call", "tools.target_preview", "tools.attempts"}
                    methods.append(method)
                    if adapter and method in {"tools.target_preview", "tools.call"}:
                        key = (params["session_id"], params["name"], json.dumps(params["arguments"], sort_keys=True))
                        if method == "tools.call":
                            prior = previews[key]
                            assert params.get("target_digest") == prior["digest"]
                            assert (params["context_id"], params["revision"]) == prior["context"]
                    reply = server.dispatch(request, transport=transports[active])
                    if adapter and method == "tools.target_preview" and reply is not None and "result" in reply:
                        target = reply["result"]["target_binding"]
                        previews[key] = {
                            "digest": target["digest"] if target else None,
                            "context": (params["context_id"], params["revision"]),
                        }
                if reply is not None:
                    transports[active].write(reply)
        except Exception as exc:
            failures.append(str(exc))
            process.kill()

    # Capture the same runtime context as a gateway reader; tool handlers retain
    # the real dispatcher's async worker/transport/profile binding.
    from agent.memory_provider import spawn_context_thread
    reader = spawn_context_thread(target=read_requests, name="owned-sdk-qualifier")
    reader.start()
    try:
        assert process.wait(timeout=90) == 0, process.stderr.read()
        reader.join(timeout=5)
        assert not reader.is_alive() and not failures, failures
        assert report == [{"mode": mode, "passed": True}]
        assert "tools.call" in methods and "tools.attempts" in methods
        if adapter:
            assert "tools.target_preview" in methods
        for session in owned_sessions.values():
            assert session["agent"]._session_messages == [{"role": "user", "content": "owned conversation"}]
            assert not session["running"]
        if mode == "roundtrip":
            for owner, session in owned_sessions.items():
                assert session["agent"]._todo_store.read()[0]["content"] == f"{owner}-todo"
            for visit, owner in enumerate(["a", "b", "a"]):
                assert (Path(roots[owner]) / f"sdk-{visit}.txt").read_text() == "effect-already-delivered"
        else:
            assert (Path(roots["a"]) / "lost.txt").read_text() == "effect-already-delivered"
            db = owned_sessions["a"]["agent"]._session_db
            assert db.get_tool_attempt("same-durable-owner", "rpc:lost-stable")["state"] == "running"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in [process.stdin, process.stdout, process.stderr]:
            stream.close()
        reader.join(timeout=5)


@pytest.mark.platforms("posix")
def test_compiled_owned_sdk_authenticated_websocket(owned_sessions, monkeypatch, request):
    """Real local ASGI route/upgrade/ticket/reattachment, not an installed provider."""
    import asyncio
    import shutil
    import socket
    import subprocess
    import time
    from fastapi import FastAPI
    import uvicorn
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    import tui_gateway.server as server
    from agent.memory_provider import spawn_context_thread

    module = request.config.getoption("--owned-sdk-module")
    node = shutil.which("node")
    if not module or not node:
        pytest.skip("cross-repository qualifier needs compiled SDK module and Node")
    adapter = request.config.getoption("--owned-dashboard-adapter")
    if adapter:
        assert Path(adapter).is_file(), "compiled Desktop adapter missing"
    home = Path(owned_sessions["a"]["profile_home"])
    (home / "ws-owned.txt").write_text("ws-owned")
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "_profile_home", lambda profile: home if profile == "a" else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    for name in ["_ensure_skin_watcher", "_ensure_lease_watcher",
                 "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"]:
        monkeypatch.setattr(server, name, lambda: None)
    app = FastAPI()
    app.include_router(chat_ws.router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(
        target=lambda: asyncio.run(service.serve(sockets=[listener])), name="owned-ws-qualifier")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        config = {"url": f"ws://127.0.0.1:{port}/api/ws",
                  "tickets": [mint_ticket(user_id="fixture-owner", provider="stub") for _ in range(2)],
                  "input": str(home / "ws-owned.txt"), "output": str(home / "ws-output.txt")}
        process = subprocess.Popen(
            [node, str(Path(__file__).parent / "fixtures" / "owned_sdk_websocket.mjs"), module, adapter or ""],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(json.dumps(config) + "\n", timeout=60)
            assert process.returncode == 0, stderr
            assert json.loads(stdout) == {"passed": True, "transport": "authenticated-websocket", "reconnect": True}
            assert (home / "ws-output.txt").read_text() == "effect-already-delivered"
            assert owned_sessions["a"]["agent"]._session_messages == [{"role": "user", "content": "owned conversation"}]
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    finally:
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()

@pytest.mark.platforms("posix")
@pytest.mark.parametrize("route", ["peer", "request.answer", "compiled-sdk", "cancel", "lost"])
def test_real_approval_queue_rejects_foreign_transport(owned_sessions, monkeypatch, request, route):
    """A guessed peer ID must not resolve another attached conversation's real queue."""
    import tui_gateway.server as server
    from tui_gateway import server_requests
    from tools import approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from agent.memory_provider import spawn_context_thread

    module = request.config.getoption("--owned-sdk-module")
    if route == "compiled-sdk":
        import shutil
        import subprocess
        node = shutil.which("node")
        if not module or not node:
            pytest.skip("compiled approval qualifier needs an SDK module and Node")
        module = Path(module).with_name("owned-gateway-approvals.js")
        assert module.is_file()

    monkeypatch.setattr(approval, "_gateway_queues", {})
    frames = queue.Queue()
    monkeypatch.setattr(server_requests, "_write", frames.put)
    results = []
    worker = spawn_context_thread(target=lambda: results.append(_await_gateway_decision(
        "same-durable-owner", lambda data: server._emit_approval_request("a", data),
        {"command": "owned qualification operation", "description": "test only", "allow_session": False, "allow_permanent": False},
    )), name="owned-approval-qualifier")
    worker.start()
    try:
        request = frames.get(timeout=10)
        frame = {"jsonrpc": "2.0", "id": request["id"], "result": {"choice": "once"}}
        if route == "compiled-sdk":
            script = """
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const { OwnedGatewayApprovals } = await import(pathToFileURL(process.argv[1]).href);
const req = JSON.parse(readFileSync(0, 'utf8'));
const custody = new OwnedGatewayApprovals();
assert.equal(custody.receive(req, true).event.session_id, 'a');
assert.equal(custody.choose(req.id, 'b', 'once'), null);
assert.equal(custody.choose(req.id, 'a', 'always'), null);
const reply = custody.choose(req.id, 'a', 'once');
assert.equal(custody.choose(req.id, 'a', 'once'), null);
assert.equal(custody.receive(req, true).reply.error.code, -32602);
console.log(JSON.stringify(reply));
"""
            completed = subprocess.run([node, "--input-type=module", "-e", script, str(module)],
                input=json.dumps(request), text=True, capture_output=True, timeout=10, check=True)
            frame = json.loads(completed.stdout)
        if route == "request.answer":
            frame = {"jsonrpc": "2.0", "id": "proxy", "method": route,
                     "params": {"id": request["id"], "result": {"choice": "once"}}}
        server.dispatch(frame, transport=owned_sessions["b"]["transport"])
        assert approval.list_gateway_approvals("same-durable-owner"), "foreign peer resolved the approval queue"
        assert server_requests.open_requests("a"), "foreign peer consumed the server request"
        invalid = {"jsonrpc": "2.0", "id": request["id"], "result": {"choice": "always"}}
        server.dispatch(invalid, transport=owned_sessions["a"]["transport"])
        assert server_requests.open_requests("a"), "unoffered choice consumed the server request"
        original = owned_sessions["a"]
        for key in ["profile_home", "session_key"]:
            value = original[key]
            original[key] = "retired-scope"
            try:
                server.dispatch(frame, transport=original["transport"])
                assert server_requests.open_requests("a"), "retired profile/session consumed the request"
            finally:
                original[key] = value
        owned_sessions["a"] = dict(original)
        try:
            server.dispatch(frame, transport=original["transport"])
            assert server_requests.open_requests("a"), "replacement session consumed an older request"
        finally:
            owned_sessions["a"] = original
        if route in {"cancel", "lost"}:
            assert worker.is_alive() and approval.list_gateway_approvals("same-durable-owner")
            server_requests.cancel("a", reason="session_closed")
            server.dispatch(frame, transport=original["transport"])
            worker.join(timeout=10)
            assert not worker.is_alive() and results[0]["choice"] is None
            assert not approval.list_gateway_approvals("same-durable-owner")
            return
        server.dispatch(frame, transport=owned_sessions["a"]["transport"])
        worker.join(timeout=10)
        assert not worker.is_alive() and results[0]["choice"] == "once"
        assert not approval.list_gateway_approvals("same-durable-owner")
    finally:
        approval.unregister_gateway_notify("same-durable-owner")
        server_requests.cancel("a", reason="session_closed")
        worker.join(timeout=5)


@pytest.mark.platforms("posix")
def test_mounted_web_desktop_cards_real_approval_queue(owned_sessions, monkeypatch, request, tmp_path):
    """Production clients + shared mounted card reach real ticket WS/approval waits."""
    import asyncio
    import shutil
    import socket
    import subprocess
    import time
    from fastapi import FastAPI
    import uvicorn
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    import tui_gateway.server as server
    from tui_gateway import server_requests
    from tools import approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from agent.memory_provider import spawn_context_thread

    module = request.config.getoption("--owned-sdk-module")
    clients = {surface: request.config.getoption(f"--approval-ui-{surface}-client") for surface in ["web", "desktop"]}
    node = shutil.which("node")
    if not node or not module or not all(clients.values()):
        pytest.skip("mounted qualifier needs compiled SDK, both production client sources and Node")
    for source in clients.values():
        assert Path(source).is_file()
    sdk = Path(module).parent.parent
    dependencies = sdk.parent.parent
    assert (dependencies / "esbuild").is_dir() and (dependencies / "jsdom").is_dir()
    assert json.loads((sdk / "package.json").read_text())["version"] == request.config.getoption("--approval-ui-sdk-version")
    home = Path(owned_sessions["a"]["profile_home"])
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "_profile_home", lambda profile: home if profile == "a" else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    for name in ["_ensure_skin_watcher", "_ensure_lease_watcher",
                 "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"]:
        monkeypatch.setattr(server, name, lambda: None)
    app = FastAPI()
    app.include_router(chat_ws.router)
    decisions, workers = [], []

    @app.post("/qualification/begin")
    async def begin():
        assert not approval.list_gateway_approvals("same-durable-owner")
        decisions.clear()
        worker = spawn_context_thread(target=lambda: decisions.append(_await_gateway_decision(
            "same-durable-owner", lambda data: server._emit_approval_request("a", data),
            {"command": "qualification-only operation", "description": "local mounted card qualification",
             "allow_session": False, "allow_permanent": False},
        )), name="mounted-approval-wait")
        workers.append(worker)
        worker.start()
        return {"started": True}

    @app.post("/qualification/state")
    async def state():
        return {"pending": bool(approval.list_gateway_approvals("same-durable-owner")),
                "choice": decisions[-1]["choice"] if decisions else None}

    @app.post("/qualification/cancel")
    async def cancel():
        server_requests.cancel("a", reason="session_closed")
        # Avoid blocking the ASGI loop while the worker finishes its real queue withdrawal.
        await asyncio.to_thread(workers[-1].join, 5)
        assert not workers[-1].is_alive()
        return {"cancelled": True}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(target=lambda: asyncio.run(service.serve(sockets=[listener])), name="mounted-approval-ws")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        config = {"http": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}/api/ws",
                  "tickets": [mint_ticket(user_id="fixture-owner", provider="stub") for _ in range(4)],
                  "dependencies": str(dependencies), "sdk": str(sdk), "clients": clients, "output": str(tmp_path)}
        completed = subprocess.run([node, str(Path(__file__).parent / "fixtures" / "owned_approval_ui.mjs")],
            input=json.dumps(config), text=True, capture_output=True, timeout=60)
        assert completed.returncode == 0, completed.stderr
        reports = json.loads(completed.stdout)
        assert reports == [{"surface": surface, "mode": mode, "passed": True}
                           for surface in ["web", "desktop"] for mode in ["once", "cancel", "disconnect"]]
        assert not approval.list_gateway_approvals("same-durable-owner")
        assert owned_sessions["a"]["agent"]._session_messages == [{"role": "user", "content": "owned conversation"}]
    finally:
        server_requests.cancel("a", reason="session_closed")
        approval.unregister_gateway_notify("same-durable-owner")
        for worker in workers:
            worker.join(timeout=5)
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive() and all(not worker.is_alive() for worker in workers)


@pytest.mark.platforms("posix")
def test_real_browser_api_owned_hermes_network(owned_sessions, monkeypatch, request, tmp_path):
    """Real WASM parents, consent UI, Hono/D1, ticket WS and actual owned handlers.

    Fixture issuer/profile mapping, provider construction, two scoped collision
    names backed by real file handlers and model completion/loopback upgrade are substituted. No stopped compute starts.
    """
    import asyncio
    import os
    import shutil
    import secrets
    import socket
    import subprocess
    import time
    from fastapi import FastAPI, Header, HTTPException
    import uvicorn
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    import tui_gateway.server as server
    from agent.memory_provider import spawn_context_thread

    # Freeze the durable history after its real initial flush, including DB
    # metadata. Background persistence may stamp an unflushed seed while the
    # longer browser qualification runs; it must not change the conversation.
    original_histories = {}
    for owner, session in owned_sessions.items():
        with session["history_lock"]:
            agent = session["agent"]
            agent._flush_messages_to_session_db(agent._session_messages)
            original_histories[owner] = copy.deepcopy(agent._session_messages)

    fund = request.config.getoption("--owned-browser-fund-root")
    node = shutil.which("node")
    if not fund or not node:
        pytest.skip("real Browser/API qualifier requires explicit Fund checkout and Node")
    fund = Path(fund).resolve()
    home = Path(owned_sessions["a"]["profile_home"])
    (home / "ws-owned.txt").write_text("ws-owned")
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    # This dedicated fixture endpoint represents A's already-selected launch profile.
    monkeypatch.setattr(server, "_profile_home", lambda profile: home if profile in (None, "a") else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    for name in ["_ensure_skin_watcher", "_ensure_lease_watcher",
                 "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"]:
        monkeypatch.setattr(server, name, lambda: None)
    app = FastAPI()
    app.include_router(chat_ws.router)
    issuer = secrets.token_urlsafe(32)

    @app.post("/qualification/ticket")
    async def fixture_ticket(x_fixture_issuer: str = Header(default="")):
        if not secrets.compare_digest(x_fixture_issuer, issuer):
            raise HTTPException(status_code=401)
        return {"ticket": mint_ticket(user_id="fixture-owner", provider="stub")}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(
        target=lambda: asyncio.run(service.serve(sockets=[listener])), name="browser-owned-qualifier")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        config = {"url": f"ws://127.0.0.1:{port}/api/ws",
                  "ticketUrl": f"http://127.0.0.1:{port}/qualification/ticket", "issuer": issuer,
                  "input": str(home / "ws-owned.txt"), "output": str(home / "ws-browser-output"),
                  "browserExecutable": request.config.getoption("--owned-browser-executable"),
                  "desktopMainModule": request.config.getoption("--owned-desktop-main-module"),
                  "desktopChatSource": request.config.getoption("--owned-desktop-chat-source"),
                  "desktopElectronMain": request.config.getoption("--owned-desktop-electron-main"),
                  "desktopElectronPreload": request.config.getoption("--owned-desktop-electron-preload"),
                  "desktopElectronExecutable": request.config.getoption("--owned-desktop-electron-executable")}
        config_path = tmp_path / "browser-fixture.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        if config["desktopChatSource"]:
            assert config["desktopMainModule"], "actual Desktop Chat qualification requires its main module"
        process = None
        try:
            outputs = []
            for qualifier_file, title, label in _qualification_groups(config):
                # Each invariant family retains its existing bounded wait. New
                # Desktop coverage must not consume the Web family's deadline.
                command = [node, str(fund / "node_modules/vitest/vitest.mjs"), "run", "--maxWorkers", "1", qualifier_file]
                if title:
                    command += ["-t", title]
                process = subprocess.Popen(
                    command,
                    cwd=fund / "apps/api", env={**os.environ, "MITHRIL_OWNED_BROWSER_FIXTURE": str(config_path),
                                                 "MITHRIL_OWNED_NATIVE_MAIN_MODULE": config["desktopMainModule"] or "",
                                                 "MITHRIL_OWNED_DESKTOP_CHAT_SOURCE": config["desktopChatSource"] or ""},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                evidence = request.config.getoption("--owned-qualification-output")
                evidence_path = None
                if evidence:
                    directory = Path(evidence)
                    assert directory.is_absolute(), "qualification evidence requires an absolute task directory"
                    evidence_path = directory / (label + ".log")
                stdout, stderr = _communicate_qualification(process, 210, evidence_path)
                assert process.returncode == 0, stdout + "\n" + stderr
                outputs.append(stdout)
            stdout = "\n".join(outputs)
            for mode in ["js-read", "python-read", "js-write", "python-write", "js-deny", "python-deny",
                         "js-alias", "python-alias"]:
                assert f"local owned browser qualified: {mode}" in stdout, stdout
            if config["desktopMainModule"]:
                for mode in ["native-read", "native-write", "native-deny"]:
                    assert f"local owned native qualified: {mode}" in stdout, stdout
                assert (home / "ws-browser-output-native-write").read_text() == "native-write"
                assert not (home / "ws-browser-output-native-deny").exists()
            if config["desktopChatSource"]:
                for mode in ["js-read", "python-read", "js-write", "python-write",
                             "js-deny", "python-deny", "js-alias", "python-alias"]:
                    assert f"local owned desktop browser qualified: {mode}" in stdout, stdout
                    if config["desktopElectronMain"]:
                        assert f"local owned electron desktop browser qualified: {mode}" in stdout, stdout
                for language in ["js", "python"]:
                    assert (home / f"ws-browser-output-desktop-{language}-write").read_text() == f"desktop-{language}-write"
                    assert not (home / f"ws-browser-output-desktop-{language}-deny").exists()
            assert (home / "ws-browser-output-js-write").read_text() == "js-write"
            assert (home / "ws-browser-output-python-write").read_text() == "python-write"
            assert not (home / "ws-browser-output-js-deny").exists()
            assert not (home / "ws-browser-output-python-deny").exists()
            attempts = owned_sessions["a"]["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"]
            expected_attempts = [
                ("read_file", "returned"), ("read_file", "returned"),
                ("web_extract", "returned"), ("web_search", "returned"),
                ("write_file", "returned"), ("write_file", "returned")]
            if config["desktopMainModule"]:
                expected_attempts += [("read_file", "returned"), ("write_file", "returned")]
            if config["desktopChatSource"]:
                expected_attempts += [("read_file", "returned"), ("read_file", "returned"),
                                      ("web_search", "returned"), ("web_extract", "returned"),
                                      ("write_file", "returned"), ("write_file", "returned")]
            assert sorted((row["tool_name"], row["state"]) for row in attempts) == sorted(expected_attempts)
            assert owned_sessions["b"]["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"] == []
            print(json.dumps({"qualified": "real-browser-api-hermes", "scenarios": 8,
                              "native_scenarios": 3 if config["desktopMainModule"] else 0,
                              "desktop_wasm_scenarios": 8 if config["desktopChatSource"] else 0,
                              "actual_attempts": len(expected_attempts), "replay_redispatches": 0, "foreign_profile_attempts": 0}))
            for owner in ["a", "b"]:
                assert owned_sessions[owner]["agent"]._session_messages == original_histories[owner]
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            config_path.unlink(missing_ok=True)
    finally:
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def test_owned_working_directory_context_retired_before_handler(owned_sessions):
    from tui_gateway.tool_snapshot import session_tool_snapshot

    session = owned_sessions["a"]
    home = Path(session["profile_home"])
    next_dir = home / "next-target"
    next_dir.mkdir()
    (next_dir / "relative.txt").write_text("new-target")
    (home / "relative.txt").write_text("original-target")
    captured = session_tool_snapshot(session)
    session["cwd"] = str(next_dir)
    stale = _call(owned_sessions, "a", "a", "read_file", {"path": str(next_dir / "relative.txt")},
                  "stale-cwd", context_id=captured["context_id"], revision=captured["revision"])
    assert stale.get("error", {}).get("code") == 4092, stale
    db = session["agent"]._session_db
    assert db.get_tool_attempt("same-durable-owner", "rpc:stale-cwd") is None
    fresh = _call(owned_sessions, "a", "a", "read_file", {"path": "relative.txt"}, "fresh-cwd")
    assert fresh["result"]["state"] == "returned", fresh
    assert "new-target" in json.dumps(fresh["result"]["output"])
    changed = session_tool_snapshot(session)
    session["cwd"] = str(home)
    restored = session_tool_snapshot(session)
    assert restored["context_id"] not in {captured["context_id"], changed["context_id"]}
    assert restored["revision"] == captured["revision"]
    stale_again = _call(owned_sessions, "a", "a", "read_file", {"path": str(next_dir / "relative.txt")},
                       "aba-cwd", context_id=captured["context_id"], revision=captured["revision"])
    assert stale_again.get("error", {}).get("code") == 4092, stale_again
    assert db.get_tool_attempt("same-durable-owner", "rpc:aba-cwd") is None
    fresh_home = _call(owned_sessions, "a", "a", "read_file", {"path": "relative.txt"}, "fresh-home-cwd")
    assert fresh_home["result"]["state"] == "returned", fresh_home
    assert "original-target" in json.dumps(fresh_home["result"]["output"])
    assert "new-target" not in json.dumps(fresh_home["result"]["output"])
    assert owned_sessions["b"]["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"] == []


@pytest.mark.parametrize("terminal_change", ["  timeout: 181\n", "  backend: ssh\n  ssh_host: example.invalid\n"])
def test_owned_terminal_policy_retires_context_without_schema_or_profile_leak(owned_sessions, terminal_change):
    from tui_gateway.tool_snapshot import session_tool_snapshot

    session, foreign = owned_sessions["a"], owned_sessions["b"]
    home = Path(session["profile_home"])
    config = home / "config.yaml"
    original_config = config.read_text()
    (home / "warm.txt").write_text("warm local runtime")
    assert _call(owned_sessions, "a", "a", "read_file", {"path": "warm.txt"}, "warm-policy")["result"]["state"] == "returned"
    captured, foreign_before = session_tool_snapshot(session), session_tool_snapshot(foreign)
    config.write_text(original_config + "terminal:\n" + terminal_change)
    target = home / "retired-policy.txt"
    stale = _call(owned_sessions, "a", "a", "write_file", {"path": str(target), "content": "must not write"},
                  "stale-terminal-policy", context_id=captured["context_id"], revision=captured["revision"])
    assert stale.get("error", {}).get("code") == 4092, stale
    db = session["agent"]._session_db
    assert not target.exists() and db.get_tool_attempt("same-durable-owner", "rpc:stale-terminal-policy") is None
    changed = session_tool_snapshot(session)
    assert changed["context_id"] != captured["context_id"] and changed["revision"] == captured["revision"]
    assert session_tool_snapshot(foreign) == foreign_before
    assert set(changed) == set(captured) and "example.invalid" not in json.dumps(changed)
    config.write_text(original_config)
    restored = session_tool_snapshot(session)
    assert restored["context_id"] not in {captured["context_id"], changed["context_id"]}
    fresh = _call(owned_sessions, "a", "a", "write_file", {"path": str(target), "content": "fresh policy"},
                  "fresh-terminal-policy")
    assert fresh["result"]["state"] == "returned" and target.read_text() == "fresh policy"
    valid = session_tool_snapshot(session)
    config.write_text("terminal: [unparseable\n")
    with pytest.raises(ValueError, match="owned terminal policy is unavailable"):
        session_tool_snapshot(session)
    config.write_text(original_config)
    repaired = session_tool_snapshot(session)
    assert repaired["context_id"] != valid["context_id"] and repaired["revision"] == valid["revision"]
    import tui_gateway.server as server
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides
    with server._session_profile_runtime_scope(session, hydrate_secrets=False):
        register_task_env_overrides(session["agent"].session_id, {"docker_image": "qualification-image"})
    try:
        override = session_tool_snapshot(session)
        assert override["context_id"] != repaired["context_id"]
        assert "qualification-image" not in json.dumps(override)
        assert session_tool_snapshot(foreign) == foreign_before
    finally:
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            clear_task_env_overrides(session["agent"].session_id)
    assert session_tool_snapshot(session)["context_id"] not in {repaired["context_id"], override["context_id"]}
    assert foreign["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"] == []


def test_owned_terminal_policy_change_during_approval_never_dispatches(owned_sessions, monkeypatch):
    session = owned_sessions["a"]
    agent, db = session["agent"], session["agent"]._session_db
    home = Path(session["profile_home"])
    target, config = home / "approval-policy.txt", home / "config.yaml"
    original_config = config.read_text()
    original_policy = agent._tool_guardrails.before_call

    def change_policy(*args, **kwargs):
        decision = original_policy(*args, **kwargs)
        config.write_text(original_config + "terminal:\n  timeout: 181\n")
        return decision

    monkeypatch.setattr(agent._tool_guardrails, "before_call", change_policy)
    arguments = {"path": str(target), "content": "must not write after approval"}
    reply = _call(owned_sessions, "a", "a", "write_file", arguments, "changed-approval-policy")
    assert reply["result"]["state"] == "rejected", reply
    assert not target.exists()
    row = db.get_tool_attempt(agent.session_id, "rpc:changed-approval-policy")
    assert row["dispatched_at"] is None and row["terminal"]
    replay = _call(owned_sessions, "a", "a", "write_file", arguments, "changed-approval-policy")
    assert replay["result"]["duplicate"] and replay["result"]["observation"] == "metadata-only"
    assert not target.exists() and len(db.list_tool_attempts(agent.session_id)["attempts"]) == 1


def test_owned_runtime_generation_shared_by_file_terminal_and_remote_code_resolver(owned_sessions):
    import tui_gateway.server as server
    from tools.code_execution_tool import _get_or_create_env
    from tools.file_tools import _get_file_ops
    from tools.terminal_tool import _acquire_env, _plan_execution

    session = owned_sessions["a"]
    home = Path(session["profile_home"])
    config = home / "config.yaml"
    original = config.read_text()
    (home / "runtime.txt").write_text("real runtime file")
    environments = []
    for visit, suffix in enumerate(["", "terminal:\n  timeout: 181\n", ""]):
        config.write_text(original + suffix)
        read = _call(owned_sessions, "a", "a", "read_file", {"path": "runtime.txt"}, f"runtime-read-{visit}")
        assert read["result"]["state"] == "returned", read
        terminal = _call(owned_sessions, "a", "a", "terminal", {"command": "printf owned-runtime"}, f"runtime-terminal-{visit}")
        assert terminal["result"]["state"] == "returned", terminal
        assert "owned-runtime" in json.dumps(terminal["result"]["output"])
        with server._session_profile_runtime_scope(session, hydrate_secrets=False):
            context = server._set_session_context(session["agent"].session_id, ui_session_id="a")
            try:
                task = "tool-only:same-durable-owner"
                file_env = _get_file_ops(task).env
                plan = _plan_execution("printf owned-runtime", task_id=task, timeout=None, background=False, _host_local=False)
                terminal_env = _acquire_env(plan, task)
                code_env, backend = _get_or_create_env(task)
                assert file_env is terminal_env is code_env and backend == "local"
                environments.append(file_env)
            finally:
                server._clear_session_context(context)
    assert len({id(env) for env in environments}) == 3
    assert owned_sessions["b"]["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"] == []


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "file-rpc"])
def test_owned_policy_generation_retires_python_namespace_without_other_profile_reset(owned_sessions, monkeypatch, kind):
    import sys
    from tools.environments.local import LocalEnvironment
    from tools import terminal_tool_backends

    initial = {owner: (Path(session["profile_home"]) / "config.yaml").read_text()
               for owner, session in owned_sessions.items()}
    if kind == "file-rpc":
        create = terminal_tool_backends._create_environment

        def local_transport(*args, **kwargs):
            if kwargs.get("env_type") == "ssh":
                # Actual local shell/process/file transport, never SSH credentials/network.
                return LocalEnvironment(cwd=kwargs["cwd"], timeout=kwargs["timeout"],
                    env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "LANG": "C.UTF-8"})
            return create(*args, **kwargs)

        monkeypatch.setattr(terminal_tool_backends, "_create_environment", local_transport)
    try:
        for owner in ["a", "b"]:
            reply = _call(owned_sessions, owner, owner, "execute_code", {"code": f"runtime_generation_marker = {owner!r}\nprint(runtime_generation_marker)"}, f"kernel-warm-{owner}")
            assert reply["result"]["state"] == "returned", reply
        config = Path(owned_sessions["a"]["profile_home"]) / "config.yaml"
        original = initial["a"]
        changes = ([original + f"terminal:\n  backend: ssh\n  ssh_host: example.invalid\n  timeout: {timeout}\n"
                    for timeout in [181, 182]] if kind == "file-rpc" else [original + "terminal:\n  timeout: 181\n"])
        for visit, policy in enumerate([*changes, original]):
            config.write_text(policy)
            reply = _call(owned_sessions, "a", "a", "execute_code", {"code": "print(globals().get('runtime_generation_marker', 'fresh-runtime'))\nruntime_generation_marker = 'retired-generation'"}, f"kernel-policy-{visit}")
            assert reply["result"]["state"] == "returned", reply
            assert "fresh-runtime" in reply["result"]["output"]["output"], reply
            foreign = _call(owned_sessions, "b", "b", "execute_code", {"code": "print(runtime_generation_marker)"}, f"kernel-foreign-{visit}")
            assert foreign["result"]["state"] == "returned" and foreign["result"]["output"]["output"].strip() == "b", foreign

    finally:
        import tui_gateway.server as server
        from tools.code_kernel import shutdown_kernels_for_owner
        from tools.code_kernel_remote import shutdown_remote_kernels_for_owner
        for session in owned_sessions.values():
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                shutdown_kernels_for_owner(session["agent"].session_id)
                shutdown_remote_kernels_for_owner(session["agent"].session_id)


@pytest.mark.parametrize("target,filename", [("memory", "MEMORY.md"), ("user", "USER.md")])
def test_owned_memory_preserves_profile_prompt_and_replay(owned_sessions, target, filename):
    # Actual inline executor, agent-owned MemoryStore, profile files and durable
    # RPC attempt journal. No external memory provider or model request.
    from tui_gateway.tool_snapshot import session_tool_snapshot

    frozen = {}
    for owner, session in owned_sessions.items():
        store = session["agent"]._memory_store
        assert store is not None
        frozen[owner] = store.format_for_system_prompt(target)
    for visit, owner in enumerate(["a", "b", "a"]):
        session = owned_sessions[owner]
        agent = session["agent"]
        home = Path(session["profile_home"])
        foreign = owned_sessions["b" if owner == "a" else "a"]
        foreign_file = Path(foreign["profile_home"]) / "memories" / filename
        foreign_before = foreign_file.read_bytes() if foreign_file.exists() else None
        definitions = copy.deepcopy(agent.tools)
        history = copy.deepcopy(agent._session_messages)
        context = session_tool_snapshot(session)
        content = f"{owner} owned memory visit {visit}"
        replacement = content + " revised"
        extra = content + " batch entry"
        operations = [
            {"action": "add", "content": content},
            {"action": "replace", "old_text": content, "content": replacement},
            {"operations": [{"action": "add", "content": extra},
                            {"action": "replace", "old_text": replacement, "content": content + " final"}]},
            {"action": "remove", "old_text": content + " final"},
        ]
        for step, args in enumerate(operations):
            request_id = f"memory-{target}-{visit}-{step}"
            reply = _call(owned_sessions, owner, owner, "memory", {"target": target, **args}, request_id)
            assert "result" in reply, reply
            result = reply["result"]
            assert result["state"] == "returned" and result["observation"] == "handler-return", result
            assert result["output"]["success"] and result["terminal"], result
        path = home / "memories" / filename
        saved = path.read_bytes()
        assert extra.encode() in saved and replacement.encode() not in saved
        replay = _call(owned_sessions, owner, owner, "memory",
                       {"target": target, **operations[0]}, f"memory-{target}-{visit}-0")["result"]
        assert replay["duplicate"] and replay["output"] is None and replay["observation"] == "metadata-only"
        assert path.read_bytes() == saved
        assert (foreign_file.read_bytes() if foreign_file.exists() else None) == foreign_before
        assert agent._memory_store.format_for_system_prompt(target) == frozen[owner]
        assert agent.tools == definitions and agent._session_messages == history
        fresh = session_tool_snapshot(session)
        assert (fresh["context_id"], fresh["revision"]) == (context["context_id"], context["revision"])
        denied = _call(owned_sessions, "b" if owner == "a" else "a", owner,
                       "memory", {"target": target, "action": "add", "content": "foreign refused"},
                       f"foreign-memory-{visit}")
        assert denied["error"]["code"] == 4001
        assert path.read_bytes() == saved


def test_owned_session_search_uses_attached_durable_store(owned_sessions):
    # Equal archive/session IDs in distinct on-disk profiles must resolve to
    # the attached agent's DB, without editing either conversation's history.
    for owner, session in owned_sessions.items():
        db = session["agent"]._session_db
        db.create_session(session_id="same-archive", source="cli", model="fixture")
        db.append_message("same-archive", "user", f"archive owned only by {owner}")
    for visit, owner in enumerate(["a", "b", "a"]):
        session = owned_sessions[owner]
        agent = session["agent"]
        history = copy.deepcopy(agent._session_messages)
        archives = {key: value["agent"]._session_db.get_messages("same-archive")
                    for key, value in owned_sessions.items()}
        args = {"session_id": "same-archive"}
        request_id = f"recall-{visit}"
        reply = _call(owned_sessions, owner, owner, "session_search", args, request_id)
        assert "result" in reply, reply
        result = reply["result"]
        assert result["state"] == "returned" and result["observation"] == "handler-return", result
        assert result["output"]["success"], result
        output = json.dumps(result["output"])
        assert f"archive owned only by {owner}" in output
        assert f"archive owned only by {'b' if owner == 'a' else 'a'}" not in output
        replay = _call(owned_sessions, owner, owner, "session_search", args, request_id)["result"]
        assert replay["duplicate"] and replay["output"] is None and replay["observation"] == "metadata-only"
        assert agent._session_messages == history
        assert {key: value["agent"]._session_db.get_messages("same-archive")
                for key, value in owned_sessions.items()} == archives


@pytest.mark.parametrize("name,attribute", [("todo_list", "_todo_store"), ("memory", "_memory_store")])
def test_owned_inline_store_replacement_retires_approval(owned_sessions, monkeypatch, name, attribute):
    from tools.todo_tool import TodoStore
    from tools.memory_tool import MemoryStore
    from tui_gateway.tool_snapshot import session_tool_snapshot

    a, b = owned_sessions["a"], owned_sessions["b"]
    approved = session_tool_snapshot(a)
    foreign = session_tool_snapshot(b)
    original = getattr(a["agent"], attribute)
    replacement = TodoStore() if name == "todo_list" else MemoryStore()
    monkeypatch.setattr(a["agent"], attribute, replacement)
    args = ({"todos": [{"id": "1", "content": "must not reach replacement", "status": "pending"}]}
            if name == "todo_list" else {"action": "add", "content": "must not reach replacement"})
    reply = _call(owned_sessions, "a", "a", name, args, "stale-inline-store",
                  context_id=approved["context_id"], revision=approved["revision"])
    if name == "todo_list":
        assert replacement.read() == [] and original.read() == [], reply
    else:
        assert replacement.memory_entries == [] and original.memory_entries == [], reply
        assert not (Path(a["profile_home"]) / "memories" / "MEMORY.md").exists()
    assert "error" in reply and reply["error"]["code"] == 4092, reply
    assert a["agent"]._session_db.get_tool_attempt("same-durable-owner", "rpc:stale-inline-store") is None
    changed = session_tool_snapshot(a)
    assert changed["context_id"] != approved["context_id"]
    assert changed["definitions"] == approved["definitions"]
    assert changed["revision"] == approved["revision"]
    assert session_tool_snapshot(b) == foreign
    monkeypatch.setattr(a["agent"], attribute, original)
    restored = session_tool_snapshot(a)
    assert restored["context_id"] not in {approved["context_id"], changed["context_id"]}
    assert restored["definitions"] == approved["definitions"]
    assert session_tool_snapshot(b) == foreign
