"""Production dashboard client against ticket WS and real persistent slash workers.

Only the outgoing approval result is dropped, after the worker committed. Node
timer/ticket adapters are qualification fixtures, not installed UI/auth proof.
"""

import asyncio
import copy
import json
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("client", ["desktop", "web"])
def test_slash_result_loss_never_redispatches_committed_memory(owned_sessions, monkeypatch, request, client):
    from fastapi import FastAPI
    import uvicorn
    from agent.memory_provider import spawn_context_thread
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    import tui_gateway.server as server
    from tui_gateway.transport import current_transport
    from tui_gateway.ws import WSTransport

    module = request.config.getoption("--owned-slash-client-module" if client == "desktop" else "--owned-slash-web-client-module")
    node = shutil.which("node")
    if not module or not node:
        pytest.skip("requires already-compiled production client and Node")
    visits, frozen, expected = [], {}, {"a": [], "b": []}
    for owner, session in owned_sessions.items():
        home = Path(session["profile_home"])
        (home / "config.yaml").write_text(json.dumps({
            "model": {"default": "qualification-no-inference", "provider": "custom",
                      "base_url": "http://127.0.0.1:9/v1"}}))
        agent = session["agent"]
        frozen[owner] = copy.deepcopy((agent._session_messages, getattr(agent, "_cached_system_prompt", None)))
        session["slash_worker"] = server._SlashWorker(
            "same-durable-owner", "qualification-no-inference", profile_home=str(home), provider="custom")
    for visit, owner in enumerate(("a", "b", "a")):
        content = f"committed-once-{owner}-{visit} 全文"
        with server._session_profile_runtime_scope(owned_sessions[owner], hydrate_secrets=False):
            pending = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": content},
                                     summary=content, origin="foreground")
        visits.append({"owner": owner, "id": pending["id"]})
        expected[owner].append(content)

    original_handler = server._methods["slash.exec"]
    original_write = WSTransport.write
    armed, lost, commands, dispatched = {}, [], [], []
    original_dispatch = server._methods["command.dispatch"]

    def dispatch(rid, params):
        dispatched.append(params)
        return original_dispatch(rid, params)

    def slash(rid, params):
        commands.append(params["command"])
        result = original_handler(rid, params)
        if params["command"].startswith("memory approve "):
            assert "result" in result, result
            armed[(id(current_transport()), rid)] = params["session_id"]
        return result

    def write(transport, frame):
        owner = armed.pop((id(transport), frame.get("id")), None)
        if owner is not None:
            with server._session_profile_runtime_scope(owned_sessions[owner], hydrate_secrets=False):
                entries = load_on_disk_store()._entries_for("memory")
                assert expected[owner][len([x for x in lost if x == owner])] in entries
            lost.append(owner)
            # Real socket close after committed handler output, without delivering its frame.
            asyncio.run_coroutine_threadsafe(transport._ws.close(code=1011), transport._loop).result(timeout=5)
            return False
        return original_write(transport, frame)

    monkeypatch.setitem(server._methods, "slash.exec", slash)
    monkeypatch.setitem(server._methods, "command.dispatch", dispatch)
    monkeypatch.setattr(WSTransport, "write", write)
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "_profile_home", lambda profile: Path(owned_sessions[profile]["profile_home"]) if profile in owned_sessions else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 600)
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher", "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    app = FastAPI()
    app.include_router(chat_ws.router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(target=lambda: asyncio.run(service.serve(sockets=[listener])), name="slash-loss-qualification")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        config = {"url": f"ws://127.0.0.1:{listener.getsockname()[1]}/api/ws", "visits": visits, "client": client,
                  "tickets": [mint_ticket(user_id="fixture-owner", provider="stub") for _ in range(6)]}
        child = subprocess.Popen([node, str(Path(__file__).parent / "fixtures/slash_response_loss.mjs"), module],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = child.communicate(json.dumps(config) + "\n", timeout=90)
            assert child.returncode == 0, stderr
            assert json.loads(stdout) == {"passed": True, "visits": 3, "redispatch": 0}
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        assert lost == ["a", "b", "a"]
        assert dispatched == []
        assert len([cmd for cmd in commands if cmd.startswith("memory approve ")]) == len(visits)
        for owner, session in owned_sessions.items():
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                assert load_on_disk_store()._entries_for("memory") == expected[owner]
                assert wa.list_pending(wa.MEMORY) == []
            agent = session["agent"]
            assert (agent._session_messages, getattr(agent, "_cached_system_prompt", None)) == frozen[owner]
    finally:
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        for owner, session in owned_sessions.items():
            server._cancel_ws_orphan_reap(owner)
            if worker := session.get("slash_worker"):
                worker.close()
                session["slash_worker"] = None
        assert not thread.is_alive()
