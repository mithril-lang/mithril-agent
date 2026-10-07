"""Owned policy bootstrap with real discovery/profile/store paths and PM boundary refusals."""
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


bootstrap = importlib.import_module("plugins.mithril-schedules.bootstrap")


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "profiles" / "a"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text("plugins:\n  enabled: [mithril-schedules]\n")
    token = set_hermes_home_override(path)
    try:
        yield path
    finally:
        reset_hermes_home_override(token)


def status(owner="owner", profile="a"):
    return {"ok": True, "receipt": {"userId": owner, "profile": profile,
                                     "selected": False, "revision": 0}}


def test_enabled_policy_prepares_actual_source_without_selecting_or_changing_bytes(home):
    (home / "cron").mkdir()
    raw = b'\xef\xbb\xbf{\r\n "jobs": []\r\n}\r\n'
    (home / "cron/jobs.json").write_bytes(raw)
    request = {"profile": "a", "nativeVersion": hashlib.sha256(raw).hexdigest()}
    calls = []
    config = (home / "config.yaml").read_bytes()
    def call(command):
        calls.append(command)
        return status()
    result = bootstrap.prepare_policy("owner", request, call)
    assert result == {"owner": "owner", **request, "bindingDigest": result["bindingDigest"]}
    assert len(result["bindingDigest"]) == 64
    assert calls == [{"action": "status", "profile": "a"}] * 2
    assert (home / "config.yaml").read_bytes() == config
    assert (home / "cron/jobs.json").read_bytes() == raw
    from cron.execution_bindings import read_execution_binding_snapshot
    assert read_execution_binding_snapshot()[0] == {}


@pytest.mark.parametrize("case", ["unauthorized", "foreign", "disabled", "override", "source_changed"])
def test_refusals_preserve_config_and_source_without_policy_confirmation(home, case):
    if case == "disabled":
        (home / "config.yaml").write_text("plugins:\n  disabled: [mithril-schedules]\n")
    elif case == "override":
        plugin = home / "plugins/mithril-schedules"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: mithril-schedules\nversion: 9.0.0\n")
    elif case in {"unauthorized", "foreign"}:
        (home / "config.yaml").write_text("plugins:\n  enabled: [other]\n")
    config = (home / "config.yaml").read_bytes()
    request = {"profile": "a", "nativeVersion": "a" * 64 if case == "source_changed" else None}
    calls = []
    def call(command):
        calls.append(command)
        return {"ok": False} if case == "unauthorized" else status("foreign" if case == "foreign" else "owner")
    with pytest.raises((ValueError, RuntimeError)):
        bootstrap.prepare_policy("owner", request, call)
    assert (home / "config.yaml").read_bytes() == config
    assert not (home / "cron/jobs.json").exists()
    assert not (home / "cron/execution-bindings.json").exists()
    assert not (home / "cron/execution-policy-required.json").exists()
    assert all(command == {"action": "status", "profile": "a"} for command in calls)


def test_admission_uses_pm_selection_cas_and_keeps_other_selections(home, monkeypatch):
    from pm import client
    from hermes_cli.plugins_admission import AdmissionRefused
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["other"], "disabled": ["unrelated"],
        "entries": {"other": {"grants": ["retained"]}},
    }}))
    before = (home / "config.yaml").read_bytes()
    submissions = []
    def refuse(**kwargs):
        submissions.append(kwargs)
        raise RuntimeError("synthetic PM refusal")
    monkeypatch.setattr(client, "sync_venv", refuse)
    with pytest.raises(AdmissionRefused):
        bootstrap.prepare_policy("owner", {"profile": "a", "nativeVersion": None}, lambda _: status())
    assert len(submissions) == 1
    selection = submissions[0]["plugins"]
    # Inspect the real PM Selection submitted by the production admission adapter.
    assert selection.data["enabled"] == ["mithril-schedules", "other"]
    assert selection.data["disabled"] == ["unrelated"]
    assert selection.data["expected_config"] == hashlib.sha256(before).hexdigest()
    assert (home / "config.yaml").read_bytes() == before
    assert not (home / "cron/jobs.json").exists()


@pytest.mark.parametrize("wire", [b'{"owner":"owner","owner":"other","prepare":{}}',
                                   b'{"owner":"owner","prepare":NaN}', b'x' * 8193,
                                   b'{"owner":"owner","prepare":{"profile":"a","nativeVersion":null}}'])
def test_actual_bootstrap_process_rejects_malformed_wire_without_source_writes(home, wire):
    import os
    env = {**os.environ, "HERMES_HOME": str(home), "MITHRIL_API_KEY": ""}
    script = Path(bootstrap.__file__)
    result = subprocess.run([sys.executable, str(script), "-p", "a", "--stdin"],
                            input=wire, capture_output=True, env=env, timeout=30)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": False, "error": "schedule_binding_unconfirmed"}
    assert not (home / "cron/jobs.json").exists()


def test_bootstrap_wire_does_not_forward_admission_diagnostics(home):
    import os
    code = '''
import importlib, sys
module = importlib.import_module("plugins.mithril-schedules.bootstrap")
def noisy(owner, request, call):
    print("synthetic resolver diagnostic")
    print("synthetic private diagnostic", file=sys.stderr)
    return {"owner": owner, **request, "bindingDigest": "a" * 64}
module.prepare_policy = noisy
sys.argv = [module.__file__, "-p", "a", "--stdin"]
module.main()
'''
    env = {**os.environ, "HERMES_HOME": str(home), "MITHRIL_API_KEY": ""}
    request = {"owner": "owner", "prepare": {"profile": "a", "nativeVersion": None}}
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps(request).encode(),
                            capture_output=True, env=env, cwd=Path(bootstrap.__file__).parents[2], timeout=30)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "receipt": {
        "owner": "owner", **request["prepare"], "bindingDigest": "a" * 64}}
    assert result.stderr == b""


def test_policy_preparation_is_private_across_a_b_a_homes(tmp_path):
    from cron.execution_bindings import read_execution_binding_snapshot
    digests = {}
    for name in ["a", "b", "a"]:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("plugins:\n  enabled: [mithril-schedules]\n")
        token = set_hermes_home_override(home)
        try:
            result = bootstrap.prepare_policy("owner_" + name, {"profile": name, "nativeVersion": None},
                                              lambda _: status("owner_" + name, name))
            policies, version = read_execution_binding_snapshot()
            assert policies == {}
            assert version == result["bindingDigest"]
            if name in digests:
                assert version == digests[name]
            digests[name] = version
            assert not (home / "cron/jobs.json").exists()
        finally:
            reset_hermes_home_override(token)
