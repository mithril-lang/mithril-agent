"""Real Web/Electron JS/Python approval to a discovered SQLite provider, A/B/A.

Owner issuance, model completions and runtime launch mapping are fixtures;
the production UI, interpreter, HTTP/D1, relay and Hermes dispatch are real.
"""

import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_holographic_routes import opened_provider
from tests.tui_gateway.test_owned_tool_call import _communicate_qualification


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("surface", ["web", "desktop"])
def test_holographic_network_consent_and_profile_custody(owned_sessions, monkeypatch, request, tmp_path, surface):
    from fastapi import FastAPI, Header, HTTPException
    import uvicorn
    from agent.memory_provider import spawn_context_thread
    import hermes_cli.web_server as web
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    import tui_gateway.server as server

    fund = request.config.getoption("--owned-browser-fund-root")
    if not fund:
        pytest.skip("requires explicit Fund checkout and browser/runtime executables")
    fund = Path(fund)
    config = {"browserExecutable": request.config.getoption("--owned-browser-executable"),
              "providerTools": "holographic", "inlineTools": True,
              "desktopMainModule": request.config.getoption("--owned-desktop-main-module"),
              "desktopChatSource": request.config.getoption("--owned-desktop-chat-source"),
              "desktopElectronMain": request.config.getoption("--owned-desktop-electron-main"),
              "desktopElectronPreload": request.config.getoption("--owned-desktop-electron-preload"),
              "desktopElectronExecutable": request.config.getoption("--owned-desktop-electron-executable")}
    if surface == "desktop":
        assert all(config[key] for key in ("desktopMainModule", "desktopChatSource", "desktopElectronMain",
                                           "desktopElectronPreload", "desktopElectronExecutable"))
        assert Path(config["desktopChatSource"]).is_file()
    else:
        for key in ("desktopMainModule", "desktopChatSource", "desktopElectronMain", "desktopElectronPreload",
                    "desktopElectronExecutable"):
            config[key] = None
    selected = ["a"]
    frozen, stores = {}, {}
    for key, session in owned_sessions.items():
        agent = session["agent"]
        agent._flush_messages_to_session_db(agent._session_messages)
        frozen[key] = copy.deepcopy((agent.tools, agent._session_messages))
        stores[key] = agent._memory_manager.providers[0]._store

    monkeypatch.setattr(web.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(chat_ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "_profile_home", lambda profile: Path(owned_sessions[profile or selected[0]]["profile_home"])
                        if (profile or selected[0]) in owned_sessions else None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 600)
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher", "_start_backend_heartbeat_refresher",
                 "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    issuer = secrets.token_urlsafe(32)
    app = FastAPI()
    app.include_router(chat_ws.router)

    def authorize(value):
        if not secrets.compare_digest(value, issuer):
            raise HTTPException(403)

    @app.post("/qualification/ticket")
    async def ticket(x_fixture_issuer: str = Header(default="")):
        authorize(x_fixture_issuer)
        return {"ticket": mint_ticket(user_id="fixture-owner", provider="stub")}

    @app.get("/qualification/inline-state")
    async def state(x_fixture_issuer: str = Header(default="")):
        authorize(x_fixture_issuer)
        owner = selected[0]
        foreign = "b" if owner == "a" else "a"
        result = {}
        for label, key in (("a", owner), ("b", foreign)):
            session = owned_sessions[key]
            result[label] = {
                "facts": [dict(row) for row in stores[key]._conn.execute(
                    "SELECT fact_id, content, category, tags, trust_score FROM facts ORDER BY fact_id")],
                "frozen": session["agent"].tools,
                "history": session["agent"]._session_messages}
        return result

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config.update(url=origin.replace("http:", "ws:") + "/api/ws", ticketUrl=origin + "/qualification/ticket", issuer=issuer)
    service = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = spawn_context_thread(target=lambda: asyncio.run(service.serve(sockets=[listener])), name="holographic-ui-qualification")
    thread.start()
    process = None
    try:
        deadline = time.monotonic() + 10
        while not service.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.started
        for cycle, owner in enumerate(("a", "b", "a")):
            selected[0] = owner
            foreign = "b" if owner == "a" else "a"
            before = {key: [tuple(row) for row in store._conn.execute("SELECT * FROM facts ORDER BY fact_id")]
                      for key, store in stores.items()}
            home = Path(owned_sessions[owner]["profile_home"])
            config.update(providerRun=f"{owner}-{cycle}", input=str(home / "unused-input"), output=str(home / "unused-output"))
            path = tmp_path / f"{surface}-{cycle}.json"
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            qualifier = "browser-owned-network.test.ts" if surface == "web" else "desktop-owned-network.test.ts"
            process = subprocess.Popen(
                [str(fund / "node_modules/.bin/vitest"), "run", f"test/{qualifier}", "--maxWorkers=1", "-t", "memory-provider"],
                cwd=fund / "apps/api", env={**os.environ, "MITHRIL_OWNED_BROWSER_FIXTURE": str(path),
                    "MITHRIL_OWNED_NATIVE_MAIN_MODULE": config["desktopMainModule"] or "",
                    "MITHRIL_OWNED_DESKTOP_CHAT_SOURCE": config["desktopChatSource"] or ""},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = request.config.getoption("--owned-qualification-output")
            evidence = None
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                evidence = directory / f"holographic-{surface}-{cycle}.log"
            stdout, stderr = _communicate_qualification(process, 180, evidence)
            assert process.returncode == 0, stdout + stderr
            label = "browser" if surface == "web" else "electron desktop browser"
            for language in ("js", "python"):
                for suffix in ("memory-provider", "memory-provider-deny"):
                    assert f"local owned {label} qualified: {language}-{suffix}" in stdout
            actual = [dict(row) for row in stores[owner]._conn.execute("SELECT content FROM facts ORDER BY fact_id")]
            expected = [f"holographic-{prior_owner}-{prior_cycle}-{surface}-{language}-memory-provider"
                        for prior_cycle, prior_owner in enumerate(("a", "b", "a")) if prior_cycle <= cycle and prior_owner == owner
                        for language in ("js", "python")]
            assert [row["content"] for row in actual] == expected
            assert [tuple(row) for row in stores[foreign]._conn.execute("SELECT * FROM facts ORDER BY fact_id")] == before[foreign]
            for key, session in owned_sessions.items():
                assert (session["agent"].tools, session["agent"]._session_messages) == frozen[key]
        for owner, session in owned_sessions.items():
            attempts = session["agent"]._session_db.list_tool_attempts("same-durable-owner")["attempts"]
            assert [(row["tool_name"], row["state"]) for row in attempts] == [("fact_store", "returned")] * (4 if owner == "a" else 2)
        print(json.dumps({"qualified": f"actual-holographic-{surface}", "visits": 3, "allow": 6, "deny": 6,
                          "foreign_effects": 0, "frozen_history_changed": False}))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        service.should_exit = True
        thread.join(timeout=10)
        listener.close()
        for owner in owned_sessions:
            server._cancel_ws_orphan_reap(owner)
        assert not thread.is_alive()
