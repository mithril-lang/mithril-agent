"""Exercise the original CLI entry point, stdin body and real profile files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

pytestmark = pytest.mark.platforms("any")


def test_original_cli_restore_uses_stdin_and_refuses_another_profile(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    for name in ("a", "b", "a"):
        home = tmp_path / name
        path = home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = {"jobs": [{"id": "original", "enabled": False, "state": "paused"}],
                    "future_header": name, "unicode_metadata": "日本語の元データ"}
        path.write_text(json.dumps(original), encoding="utf-8")
        request = {"owner": "alice", "profile": "default", "operationId": "cli-" + name,
                   "expectedVersion": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "file": {**original, "future_header": name + " restored"}}
        # Desktop writes UTF-8 bytes to a pipe, regardless of Windows' locale.
        env = {**os.environ, "HERMES_HOME": str(home), "PYTHONIOENCODING": "ascii:backslashreplace"}
        # Unique native operations: a later write is a new restore, not a replay
        # of the first profile A acknowledgement.
        if name == "a" and list(path.parent.glob(".mithril-source-*.json")):
            request["operationId"] += "-second"
        result = subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-restore"],
            input=json.dumps(request, ensure_ascii=False), encoding="utf-8", capture_output=True, env=env, cwd=checkout, timeout=30)
        assert result.returncode == 0, result.stderr
        reply = json.loads(result.stdout)
        assert reply["success"] is True
        assert reply["receipt"]["version"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert json.loads(path.read_bytes()) == request["file"]
        before = path.read_bytes()
        request["profile"] = "another-profile"
        refused = subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-restore"],
            input=json.dumps(request, ensure_ascii=False), encoding="utf-8", capture_output=True, env=env, cwd=checkout, timeout=30)
        assert refused.returncode == 1
        assert json.loads(refused.stdout) == {"success": False, "error": "identity"}
        assert path.read_bytes() == before


def test_original_cli_source_text_retains_bytes_and_replays_without_overwriting_newer_edits(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    for index, name in enumerate(("a", "b", "a")):
        home = tmp_path / name
        path = home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b'{"jobs": []}\r\n')
        source = '\ufeff{\r\n "opaqueCounter": 9223372036854775807, "profileLabel": "' + name + '",\r\n "jobs": [{"id":"original","enabled":false,"state":"paused","future":"日本語"}]\r\n}\r\n'
        request = {"owner": "alice", "profile": "default", "operationId": "raw-" + str(index),
                   "expectedVersion": hashlib.sha256(path.read_bytes()).hexdigest(), "sourceText": source}
        env = {**os.environ, "HERMES_HOME": str(home), "PYTHONIOENCODING": "ascii:backslashreplace"}
        def invoke(body):
            return subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-restore"],
                input=json.dumps(body, ensure_ascii=False).encode("utf-8"), capture_output=True, env=env, cwd=checkout, timeout=30)
        result = invoke(request)
        assert result.returncode == 0, result.stderr
        reply = json.loads(result.stdout)
        assert path.read_bytes() == source.encode("utf-8")
        assert reply["receipt"]["version"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
        newer = source.replace('"profileLabel": "' + name + '"', '"profileLabel": "newer"')
        path.write_bytes(newer.encode("utf-8"))
        replay = invoke(request)
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout) == reply
        assert path.read_bytes() == newer.encode("utf-8")
        for bad in ({**request, "operationId": "mixed", "file": {"jobs": []}},
                    {**request, "operationId": "duplicate", "sourceText": '{"jobs":[],"jobs":[]}'},
                    {**request, "operationId": "bad", "sourceText": '{"jobs":[],"value":NaN}'},
                    {**request, "operationId": "overflow", "sourceText": '{"jobs":[],"value":1e999}'},
                    {**request, "operationId": "profile", "profile": "other"}):
            refused = invoke(bad)
            assert refused.returncode == 1
            assert path.read_bytes() == newer.encode("utf-8")
