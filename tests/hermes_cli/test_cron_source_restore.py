"""Exercise the original CLI entry point, stdin body and real profile files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def test_original_cli_restore_uses_stdin_and_refuses_another_profile(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    for name in ("a", "b", "a"):
        home = tmp_path / name
        path = home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = {"jobs": [{"id": "original", "enabled": False, "state": "paused"}], "future_header": name}
        path.write_text(json.dumps(original))
        request = {"owner": "alice", "profile": "default", "operationId": "cli-" + name,
                   "expectedVersion": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "file": {**original, "future_header": name + " restored"}}
        env = {**os.environ, "HERMES_HOME": str(home)}
        # Unique native operations: a later write is a new restore, not a replay
        # of the first profile A acknowledgement.
        if name == "a" and list(path.parent.glob(".mithril-source-*.json")):
            request["operationId"] += "-second"
        result = subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-restore"],
            input=json.dumps(request), text=True, capture_output=True, env=env, cwd=checkout, timeout=30)
        assert result.returncode == 0, result.stderr
        reply = json.loads(result.stdout)
        assert reply["success"] is True
        assert reply["receipt"]["version"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert json.loads(path.read_bytes()) == request["file"]
        before = path.read_bytes()
        request["profile"] = "another-profile"
        refused = subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-restore"],
            input=json.dumps(request), text=True, capture_output=True, env=env, cwd=checkout, timeout=30)
        assert refused.returncode == 1
        assert json.loads(refused.stdout) == {"success": False, "error": "identity"}
        assert path.read_bytes() == before
