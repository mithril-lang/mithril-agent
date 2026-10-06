"""Real discovery, profile routing and HTTP contracts for the owned Code plugin."""
import json
import threading
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@contextmanager
def runner():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), None))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ready": True, "mode": "local-jeV-mithril-harness"}).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            if body["goal"] == "redirect":
                self.send_response(307)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            result = {"format": "mithril.code-project/v1", "verified": body["goal"] != "unverified",
                      "metrics": {"receipt-id": "synthetic-adapter-test"}, "logic": {},
                      "files": {"src/todo/interaction.cljk": "toggle", "src/todo/summary.cljk": "count"}}
            self.wfile.write(json.dumps(result).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_discovery_and_real_http_are_profile_scoped(tmp_path):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    with runner() as (url, calls):
        homes = [tmp_path / "a", tmp_path / "b"]
        for home in homes:
            home.mkdir()
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [mithril-code]\n  entries:\n    mithril-code:\n      settings:\n        runner_url: " + url + "\n")
        set_multiplex_active(True)
        try:
            for home, credential in [(homes[0], "a" * 32), (homes[1], "b" * 32), (homes[0], "a" * 32)]:
                ht = set_hermes_home_override(home)
                st = set_secret_scope({"CODE_RUNNER_TOKEN": credential}, profile_home=str(home))
                try:
                    manager = PluginManager()
                    manager.discover_and_load()
                    assert manager._plugins["mithril-code"].enabled
                    entry = registry.get_entry("mithril_code", scope=manager.scope_key)
                    assert entry.check_fn()
                    assert json.loads(entry.handler({"action": "status"}))["ready"]
                    result = json.loads(entry.handler({"action": "run", "goal": "bounded todo"}))
                    assert result["ok"] and result["result"]["verified"]
                    assert calls[-1][1] == "Bearer " + credential
                    assert calls[-1][2] == {"template": "todo", "goal": "bounded todo"}
                    command = next(c for c in manager._cli_commands.values() if c["name"] == "mithril-code")
                    assert command["handler_fn"] is not None
                finally:
                    manager.unload()
                    reset_secret_scope(st)
                    reset_hermes_home_override(ht)
            home = homes[0]
            command = [sys.executable, str(Path(__file__).resolve().parents[2] / "hermes"), "mithril-code", "run", "--stdin"]
            process = subprocess.run(command, input=json.dumps({"goal": "cli todo"}), text=True,
                env={"HERMES_HOME": str(home), "CODE_RUNNER_TOKEN": "a" * 32, "PATH": "/usr/bin:/bin"},
                capture_output=True, timeout=20)
            assert process.returncode == 0, process.stderr
            assert json.loads(process.stdout.strip().split("\n")[-1])["ok"]
            assert calls[-1][2] == {"template": "todo", "goal": "cli todo"}
        finally:
            set_multiplex_active(False)


def test_redirect_unverified_result_and_invalid_goal_do_not_run_again(tmp_path):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "plugins/mithril-code/client.py"
    spec = importlib.util.spec_from_file_location("mithril_code_client_test", path)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    with runner() as (url, calls):
        for goal in ("redirect", "unverified"):
            result = client.call_runner(url, "c" * 32, "run", goal)
            assert result["ok"] is False
        assert len(calls) == 2
        assert not any(path == "/credential-leak" for path, _, _ in calls)
        assert not client.call_runner(url, "c" * 32, "run", "")["ok"]
        assert not client.call_runner("http://example.com", "c" * 32, "run", "todo")["ok"]
        assert len(calls) == 2
