"""Real bundled RetainDB client to a local HTTP fixture; no vendor account proof."""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call


def opened_provider(path):
    from plugins.memory import load_memory_provider
    provider = load_memory_provider("retaindb", register_skills=False)
    assert provider is not None
    provider.initialize("same-durable-owner", hermes_home=str(path.parent), user_id=path.parent.name)
    return provider


@pytest.fixture(autouse=True)
def local_http(monkeypatch):
    import plugins.memory.retaindb as retaindb
    records = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()
        def do_POST(self):
            self.respond()
        def respond(self):
            parsed = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            records.append({"method": self.command, "path": parsed.path,
                            "project": body.get("project") or parse_qs(parsed.query).get("project", [None])[0],
                            "body": body})
            payload = json.dumps({"result": "local HTTP fixture", "memories": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(retaindb, "get_secret", lambda name, default=None:
                        "fixture-only-key" if name == "RETAINDB_API_KEY" else default)
    monkeypatch.setattr(retaindb, "_load_retaindb_config", lambda:
                        {"base_url": f"http://127.0.0.1:{server.server_port}"})
    try:
        yield records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
@pytest.mark.parametrize("name,args", [
    ("retaindb_remember", {"content": "owned local HTTP fact"}),
    ("retaindb_search", {"query": "owned local HTTP fact"}),
    ("retaindb_profile", {}),
])
def test_owned_retaindb_project_cannot_redirect(local_http, owned_sessions, monkeypatch, stage, name, args):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers = {key: row["agent"]._memory_manager.providers[0] for key, row in owned_sessions.items()}
    projects = {key: provider._client.project for key, provider in providers.items()}
    assert projects["a"] != projects["b"]
    frozen = {key: copy.deepcopy((row["agent"].tools, row["history"])) for key, row in owned_sessions.items()}
    for visit, owner in enumerate(("a", "b", "a")):
        foreign = "b" if owner == "a" else "a"
        provider = providers[owner]
        before = copy.deepcopy(local_http)
        snapshot = session_tool_snapshot(owned_sessions[owner])
        def redirect(*, args, next_call=None, **kwargs):
            provider._client.project = projects[foreign]
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)
        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [redirect]
        request_id = f"rdb-stale-{name}-{stage}-{visit}"
        try:
            result = _call(owned_sessions, owner, owner, name, args, request_id)["result"]
            assert local_http == before, local_http[len(before):]
            assert result["state"] == "rejected" and result["output"] is None, result
            changed = session_tool_snapshot(owned_sessions[owner])
            assert changed["context_id"] != snapshot["context_id"]
            attempt = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
            assert attempt["dispatched_at"] is None
        finally:
            collection.clear()
            provider._client.project = projects[owner]
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {snapshot["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, name, args, request_id)["result"]
        assert replay["duplicate"] and replay["output"] is None and local_http == before
        fresh = _call(owned_sessions, owner, owner, name, args, f"rdb-fresh-{name}-{stage}-{visit}")["result"]
        assert fresh["state"] == "returned", fresh
        assert len(local_http) == len(before) + 1 and local_http[-1]["project"] == projects[owner]
        duplicate = _call(owned_sessions, owner, owner, name, args, f"rdb-fresh-{name}-{stage}-{visit}")["result"]
        assert duplicate["duplicate"] and duplicate["output"] is None and len(local_http) == len(before) + 1
        for key, row in owned_sessions.items():
            assert (row["agent"].tools, row["history"]) == frozen[key]
