"""Actual sandboxed Electron hook/panel to ticket WS and persistent workers.

Launch/profile URL issuance and renderer shell are fixtures. Agent construction
is offline; no inference, original user profile or installed app is accessed.
"""

import asyncio
import copy
import json
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401


@pytest.mark.platforms("posix")
def test_electron_memory_review_keeps_profile_and_result_custody(owned_sessions, monkeypatch, request, tmp_path):
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse
    import uvicorn
    from agent.memory_provider import spawn_context_thread
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store, MemoryStore
    from tools.write_approval_decisions import pending_decision_lock, write_receipt, decision_receipt
    import tui_gateway.server as server
    from tui_gateway.transport import current_transport
    from tui_gateway.ws import WSTransport

    desktop = request.config.getoption("--owned-memory-electron-root")
    build = request.config.getoption("--owned-memory-electron-build")
    executable = request.config.getoption("--owned-desktop-electron-executable")
    if not desktop or not build or not executable:
        pytest.skip("requires explicit Desktop checkout, prebuilt fixture and Electron executable")
    desktop, build = Path(desktop), Path(build)
    for name in ("main.cjs", "preload.cjs", "renderer.js", "renderer.css", "index.html"):
        assert (build / name).is_file()
    frozen, histories, expected = {}, {}, {"a": [], "b": []}
    for owner, session in owned_sessions.items():
        home = Path(session["profile_home"])
        (home / "config.yaml").write_text(json.dumps({
            "model": {"default": "qualification-no-inference", "provider": "custom", "base_url": "http://127.0.0.1:9/v1"}}))
        agent = session["agent"]
        agent._flush_messages_to_session_db(agent._session_messages)
        histories[owner] = copy.deepcopy(agent._session_messages)
        frozen[owner] = {key: agent._memory_store.format_for_system_prompt(key) for key in ("memory", "user")}
        session["slash_worker"] = server._SlashWorker("same-durable-owner", "qualification-no-inference", profile_home=str(home), provider="custom")

    proposals = []
    for cycle, owner in enumerate(("a", "b", "a")):
        for target, decision in (("memory", "approve"), ("user", "reject")):
            content = f"electron-{owner}-{cycle}-{target}: " + "full text " * 20 + f"\n全文の末尾-{owner}-{cycle}-{target} <script>not executed</script>"
            with server._session_profile_runtime_scope(owned_sessions[owner], hydrate_secrets=False):
                record = wa.stage_write(wa.MEMORY, {"action": "add", "target": target, "content": content}, summary=f"electron-{owner}-{cycle}-{target}", origin="foreground")
            proposals.append({"owner": owner, "cycle": cycle, "target": target, "decision": decision,
                              "id": record["id"], "summary": record["summary"], "content": content,
                              "lost": owner == "a" and cycle == 2 and target == "memory"})
            if decision == "approve":
                expected[owner].append(content)
    with server._session_profile_runtime_scope(owned_sessions["a"], hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {"action": "add", "target": "memory", "content": "retired full body"}, summary="retired proposal", origin="foreground")
    retired = {"owner": "a", "cycle": -1, "target": "memory", "decision": "reject", "id": record["id"], "summary": record["summary"], "content": "retired full body"}
    recovery = []
    saved = {"a": [], "b": []}
    for cycle, owner in enumerate(("a", "b", "a")):
        for target, choice in (("memory", "saved"), ("user", "unsaved")):
            content = f"uncertain-{owner}-{cycle}-{target}: 全文の保存結果"
            with server._session_profile_runtime_scope(owned_sessions[owner], hydrate_secrets=False):
                record = wa.stage_write(wa.MEMORY, {"action": "add", "target": target, "content": content}, summary=content, origin="foreground")
                with pending_decision_lock(wa.MEMORY, record["id"]):
                    write_receipt(wa.MEMORY, record, "approve", "applying")
                    if choice == "saved":
                        assert MemoryStore().add(target, content)["success"]
                        saved[owner].append(content)
            recovery.append({"owner": owner, "cycle": cycle, "target": target, "decision": "resolve-" + choice,
                             "id": record["id"], "summary": record["summary"], "content": content,
                             "lost": cycle == 2 and target == "user"})
    with server._session_profile_runtime_scope(owned_sessions["a"], hydrate_secrets=False):
        with pending_decision_lock(wa.MEMORY, retired["id"]):
            write_receipt(wa.MEMORY, wa.get_pending(wa.MEMORY, retired["id"]), "approve", "unknown")
    retired["decision"] = "resolve-unsaved"
    for owner in expected:
        expected[owner] = saved[owner] + expected[owner]
    lost_id = next(row["id"] for row in proposals if row["lost"])
    lost_commands = {f"memory approve {lost_id}": "electron-approve",
                     f"memory resolve-unsaved {recovery[-1]['id']}": "electron-closure"}
    proposals.extend(recovery)
    closure_snapshots = []
    delayed, release = threading.Event(), threading.Event()
    lost = []
    frames, armed = [], {}
    original_write = WSTransport.write
    originals = {name: server._methods[name] for name in ("slash.exec", "command.dispatch", "prompt.submit", "session.create", "session.resume")}

    def record_method(name, handler):
        def handle(rid, params):
            frames.append((name, dict(params)))
            command = params.get("command", "").lstrip("/")
            before = None
            if command.startswith(("memory resolve-saved ", "memory resolve-unsaved ")):
                with server._session_profile_runtime_scope(server._sessions[params["session_id"]], hydrate_secrets=False):
                    before = {key: load_on_disk_store().review_state(key) for key in ("memory", "user")}
            result = handler(rid, params)
            if before is not None:
                with server._session_profile_runtime_scope(server._sessions[params["session_id"]], hydrate_secrets=False):
                    after = {key: load_on_disk_store().review_state(key) for key in ("memory", "user")}
                assert after == before
                closure_snapshots.append(before)
            if command == f"memory review {retired['id']}" and not delayed.is_set():
                delayed.set()
                assert release.wait(timeout=15)
            key = " ".join(command.split()[:3])
            if key in lost_commands:
                assert "result" in result, result
                armed[(id(current_transport()), rid)] = lost_commands[key]
            return result
        return handle

    for name, handler in originals.items():
        monkeypatch.setitem(server._methods, name, record_method(name, handler))

    def write(transport, frame):
        marker = armed.pop((id(transport), frame.get("id")), None)
        if marker:
            with server._session_profile_runtime_scope(owned_sessions["a"], hydrate_secrets=False):
                assert load_on_disk_store()._entries_for("memory") == expected["a"]
            lost.append(marker)
            asyncio.run_coroutine_threadsafe(transport._ws.close(code=1011), transport._loop).result(timeout=5)
            return False
        return original_write(transport, frame)

    monkeypatch.setattr(WSTransport, "write", write)
    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    web_owner = [None]
    monkeypatch.setattr(server, "_profile_home", lambda profile: Path(owned_sessions[profile or web_owner[0]]["profile_home"]) if (profile or web_owner[0]) in owned_sessions else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 600)
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher", "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    issuer = secrets.token_urlsafe(32)
    app = FastAPI()
    app.include_router(chat_ws.router)

    def authorize(value):
        if value != issuer:
            raise HTTPException(403)

    @app.post("/qualification/ticket")
    async def ticket(x_qualification_issuer: str = Header(default=""), x_fixture_issuer: str = Header(default="")):
        authorize(x_qualification_issuer or x_fixture_issuer)
        return {"ticket": mint_ticket(user_id="fixture-owner", provider="stub")}

    @app.get("/qualification/state")
    async def state(x_qualification_issuer: str = Header(default="")):
        authorize(x_qualification_issuer)
        return {"delayed": delayed.is_set(), "lost": bool(lost)}

    @app.post("/qualification/release")
    async def unblock(x_qualification_issuer: str = Header(default="")):
        authorize(x_qualification_issuer)
        release.set()
        return {"released": True}

    @app.get("/qualification/renderer/{name}")
    async def renderer(name: str):
        if name not in {"index.html", "renderer.js", "renderer.css"}:
            raise HTTPException(404)
        return FileResponse(build / name)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(target=lambda: asyncio.run(service.serve(sockets=[listener])), name="memory-electron-qualification")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        config = {"origin": f"http://127.0.0.1:{listener.getsockname()[1]}", "issuer": issuer,
                  "build": str(build), "preload": str(build / "preload.cjs"), "userData": str(tmp_path / "electron-user-data"),
                  "executable": executable, "proposals": proposals, "retired": retired}
        fixture = tmp_path / "electron-config.json"
        fixture.write_text(json.dumps(config))
        import os
        env = os.environ.copy()
        env["MITHRIL_MEMORY_ELECTRON_FIXTURE"] = str(fixture)
        child = subprocess.Popen([str(desktop / "node_modules/.bin/vitest"), "run", "tests/memory-review-electron.test.ts", "--maxWorkers=1"],
                                 cwd=desktop, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = child.communicate(timeout=110)
            output = request.config.getoption("--owned-qualification-output")
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                (Path(output) / "memory-review-electron.log").write_text(stdout + stderr)
            assert child.returncode == 0, stdout + stderr
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        web_rows = []
        fund = request.config.getoption("--owned-browser-fund-root")
        if fund:
            fund = Path(fund)
            for cycle, owner in enumerate(("a", "b", "a")):
                rows = []
                with server._session_profile_runtime_scope(owned_sessions[owner], hydrate_secrets=False):
                    for target, choice in (("memory", "saved"), ("user", "unsaved")):
                        content = f"web-uncertain-{owner}-{cycle}-{target}: 全文の保存結果"
                        record = wa.stage_write(wa.MEMORY, {"action": "add", "target": target, "content": content}, summary=content, origin="foreground")
                        with pending_decision_lock(wa.MEMORY, record["id"]):
                            write_receipt(wa.MEMORY, record, "approve", "applying")
                            if choice == "saved":
                                assert MemoryStore().add(target, content)["success"]
                                expected[owner].append(content)
                        rows.append({"owner": owner, "id": record["id"], "target": target, "decision": "resolve-" + choice, "content": content, "lost": cycle == 2 and target == "user"})
                if cycle == 2:
                    lost_commands[f"memory resolve-unsaved {rows[-1]['id']}"] = "web-closure"
                web_owner[0] = owner
                web_config = tmp_path / f"web-recovery-{cycle}.json"
                web_config.write_text(json.dumps({"url": config["origin"].replace("http:", "ws:") + "/api/ws",
                    "ticketUrl": config["origin"] + "/qualification/ticket", "issuer": issuer,
                    "browserExecutable": request.config.getoption("--owned-browser-executable"), "reviewProposals": rows}))
                env["MITHRIL_OWNED_BROWSER_FIXTURE"] = str(web_config)
                completed = subprocess.run([str(fund / "node_modules/.bin/vitest"), "run", "test/memory-review-network.test.ts", "--maxWorkers=1"],
                    cwd=fund / "apps/api", env=env, capture_output=True, text=True, timeout=110)
                if output:
                    (Path(output) / f"memory-recovery-web-{cycle}.log").write_text(completed.stdout + completed.stderr)
                assert completed.returncode == 0, completed.stdout + completed.stderr
                web_rows.extend(rows)
        assert delayed.is_set() and lost == ["electron-approve", "electron-closure"] + (["web-closure"] if fund else [])
        assert all(name not in {"prompt.submit", "session.create", "command.dispatch"} for name, _ in frames)
        decisions = [params["command"].lstrip("/") for name, params in frames if name == "slash.exec" and params.get("command", "").lstrip("/").startswith(("memory approve ", "memory reject ", "memory resolve-saved ", "memory resolve-unsaved "))]
        for row in proposals + [retired] + web_rows:
            assert sum(command.startswith(f"memory {row['decision']} {row['id']} ") for command in decisions) == 1
        assert len(closure_snapshots) == 7 + len(web_rows)
        for row in recovery + [retired] + web_rows:
            with server._session_profile_runtime_scope(owned_sessions[row["owner"]], hydrate_secrets=False):
                assert decision_receipt(wa.MEMORY, row["id"])["resolution"] == row["decision"].removeprefix("resolve-")
        for owner, session in owned_sessions.items():
            with server._session_profile_runtime_scope(session, hydrate_secrets=False):
                store = load_on_disk_store()
                assert store._entries_for("memory") == expected[owner]
                assert store._entries_for("user") == []
                assert wa.list_pending(wa.MEMORY) == []
            agent = session["agent"]
            assert agent._session_messages == histories[owner]
            assert {key: agent._memory_store.format_for_system_prompt(key) for key in frozen[owner]} == frozen[owner]
    finally:
        release.set()
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        for owner, session in owned_sessions.items():
            server._cancel_ws_orphan_reap(owner)
            if worker := session.get("slash_worker"):
                worker.close()
                assert worker.proc.poll() is not None
                session["slash_worker"] = None
        assert not thread.is_alive()
