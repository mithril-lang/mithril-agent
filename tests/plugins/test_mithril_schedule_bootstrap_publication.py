"""Actual PM worker publication before original policy preparation, across profile homes."""
import hashlib
import importlib
import json

import pytest

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tests.pm.test_worker import client, isolated_python, _current_environment  # noqa: F401


@pytest.mark.parametrize("concurrent", [False, True])
def test_owned_bootstrap_uses_real_pm_worker_and_config_cas(client, tmp_path, monkeypatch, concurrent):
    from pm import receipt
    from hermes_cli.plugins_admission import AdmissionRefused
    from utils import fast_safe_load
    from cron.execution_bindings import read_execution_binding_snapshot
    from hermes_cli import plugins_activation
    module = importlib.import_module("plugins.mithril-schedules.bootstrap")
    # Existing environment with no additional dependency members: the owned
    # policy has no python_dependencies. The real worker must still publish
    # the selection and receipt; no application config callback is substituted.
    _current_environment(tmp_path, monkeypatch, [])
    calls = []
    activations = []
    # Keep the real activation/rendezvous path. Observe its ordering without
    # connecting to or restarting the user's installed runtime.
    activate = plugins_activation.activate_plugin_now
    def observe_activation(name, **kwargs):
        assert read_execution_binding_snapshot()[0] == {}
        activations.append((name, kwargs))
        return activate(name, **kwargs)
    monkeypatch.setattr(plugins_activation, "activate_plugin_now", observe_activation)
    for name in ["a", "b", "a"]:
        home = tmp_path / "home/profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        config = home / "config.yaml"
        if not config.exists():
            config.write_text('# retained comment\nmodel: "unchanged"\nplugins:\n'
                              '  enabled: [other]\n  disabled: [unrelated]\n'
                              '  entries:\n    other:\n      grants: [retained]\n')
        (home / "cron").mkdir(exist_ok=True)
        source = home / "cron/jobs.json"
        raw = b'\xef\xbb\xbf{\r\n "jobs": []\r\n}\r\n'
        source.write_bytes(raw)
        request = {"profile": name, "nativeVersion": hashlib.sha256(raw).hexdigest()}
        scope = set_hermes_home_override(home)
        try:
            def status(command):
                calls.append(command)
                if concurrent:
                    config.write_text('plugins:\n  disabled: [mithril-schedules]\n')
                return {"ok": True, "receipt": {"userId": "owner_" + name, "profile": name,
                                                 "selected": False, "revision": 0}}
            with receipt.worker_context("owned-bootstrap-" + name):
                if concurrent:
                    with pytest.raises(AdmissionRefused):
                        module.prepare_policy("owner_" + name, request, status)
                    assert config.read_text() == 'plugins:\n  disabled: [mithril-schedules]\n'
                    assert not (home / "cron/execution-policy-required.json").exists()
                    assert not (home / "cron/execution-bindings.json").exists()
                else:
                    result = module.prepare_policy("owner_" + name, request, status)
                    value = fast_safe_load(config.read_text())
                    assert value["plugins"] == {"enabled": ["mithril-schedules", "other"],
                                                "disabled": ["unrelated"],
                                                "entries": {"other": {"grants": ["retained"]}}}
                    assert value["model"] == "unchanged"
                    assert '# retained comment' in config.read_text()
                    policies, version = read_execution_binding_snapshot()
                    assert policies == {}
                    assert version == result["bindingDigest"]
                    record = json.loads((home / "logs/update_receipts/latest.json").read_text())
                    assert record["outcome"] == "ok"
            assert source.read_bytes() == raw
        finally:
            reset_hermes_home_override(scope)
        if concurrent:
            break
    assert all(call["action"] == "status" for call in calls)
    assert activations == ([] if concurrent else [
        ("mithril-schedules", {"in_process": False}),
        ("mithril-schedules", {"in_process": False}),
    ])
