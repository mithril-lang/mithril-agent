"""Use the actual original CLI to prepare schedules without native file changes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("any")


def test_readonly_original_preparation_is_profile_and_timezone_bound(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    for name, plan, kind in [("a", "every monday 9am", "cron"),
                              ("b", "2h", "interval"), ("a", "in 30m", "once")]:
        home = tmp_path / name
        path = home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": [{"id": "keep", "future_field": name}],
                                    "original_metadata": ["日本語", name]}), encoding="utf-8")
        before = path.read_bytes()
        request = {"owner": "alice", "profile": "default", "operationId": "prepare-" + name,
                   "timeZone": "Asia/Tokyo", "input": {"schedule": plan,
                   "prompt": "日本語の元の作業", "name": "Original", "deliver": "local"}}
        env = {**os.environ, "HERMES_HOME": str(home), "HERMES_TIMEZONE": "Asia/Tokyo",
               "PYTHONIOENCODING": "ascii:backslashreplace"}

        def call(wire):
            return subprocess.run([sys.executable, str(checkout / "hermes"), "cron", "source-prepare"],
                input=wire, encoding="utf-8", capture_output=True, env=env, cwd=checkout, timeout=30)

        result = call(json.dumps(request, ensure_ascii=False))
        assert result.returncode == 0, result.stderr
        reply = json.loads(result.stdout)
        preparation = reply["preparation"]
        assert {key: preparation[key] for key in request} == request
        assert preparation["job"]["schedule"]["kind"] == kind
        assert preparation["job"]["prompt"] == request["input"]["prompt"]
        assert preparation["job"]["deliver"] == "local"
        assert json.loads(preparation["sourceText"]) == preparation["job"]
        assert request["input"]["prompt"] in preparation["sourceText"]
        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == ["jobs.json"]
        if kind == "once":
            assert preparation["job"]["schedule"]["run_at"].endswith("+09:00")

        for changed, error in [({"profile": "other"}, "identity"),
                               ({"timeZone": "UTC"}, "timezone"),
                               ({"input": {**request["input"], "script": "unexpected"}}, "operation")]:
            refused = call(json.dumps({**request, **changed}, ensure_ascii=False))
            assert refused.returncode == 1
            assert json.loads(refused.stdout) == {"success": False, "error": error}
            assert path.read_bytes() == before
        duplicate = json.dumps(request).replace('"owner": "alice"', '"owner": "alice", "owner": "bob"')
        refused = call(duplicate)
        assert refused.returncode == 1
        assert json.loads(refused.stdout) == {"success": False, "error": "operation"}
        assert path.read_bytes() == before
