"""Real original inventory fences and required-policy restoration boundaries."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cron import jobs, source_restore
from cron.execution_bindings import (
    install_execution_bindings, read_execution_binding_snapshot, read_execution_bindings,
    require_execution_policy, resolve_execution_binding,
)


def test_required_policy_preparation_preserves_original_source_and_existing_bindings(tmp_path):
    path = tmp_path / 'cron' / 'jobs.json'
    with jobs.use_cron_store(tmp_path):
        version = require_execution_policy(None, None)
        assert not path.exists()
        assert read_execution_bindings() == {}
        assert require_execution_policy(None, version) == version
        source = '\ufeff{"jobs":[{"id":"one","enabled":true,"state":"scheduled"}],"opaque":9223372036854775807}\r\n'
        source_restore.restore_original_store(owner='alice', profile='default', operation_id='one',
            expected_version=None, source_text=source)
        before = path.read_bytes()
        with pytest.raises(ValueError, match='binding_unconfirmed'):
            resolve_execution_binding({'id': 'one'})
        native = hashlib.sha256(before).hexdigest()
        bindings = {'one': {'policy': 'test-policy', 'opaque': {'definition': 42}}}
        installed = install_execution_bindings(bindings, native, version)
        assert require_execution_policy(native, installed) == installed
        assert read_execution_bindings() == bindings and path.read_bytes() == before
        for wrong_source, wrong_binding in [(None, installed), (native, 'a'*64)]:
            with pytest.raises(ValueError, match='changed'):
                require_execution_policy(wrong_source, wrong_binding)
        assert path.read_bytes() == before
        assert read_execution_binding_snapshot() == (bindings, installed)


def test_missing_or_damaged_required_policy_never_permits_active_original_restoration(tmp_path):
    with jobs.use_cron_store(tmp_path):
        require_execution_policy(None, None)
        path = tmp_path / 'cron' / 'execution-bindings.json'
        retained = path.read_bytes()
        for data in [None, b'{invalid', b'{"schemaVersion":1,"bindings":{},"extra":true}']:
            if data is None:
                path.unlink()
            else:
                path.write_bytes(data)
                path.chmod(0o600)
            with pytest.raises(ValueError):
                source_restore.restore_original_store(owner='alice', profile='default', operation_id='refused',
                    expected_version=None, source_text='[{"id":"new","enabled":true,"state":"scheduled"}]')
            assert not (tmp_path / 'cron' / 'jobs.json').exists()
            path.write_bytes(retained)
            path.chmod(0o600)
        # Repairing a marker-only crash preserves the required lane; it never activates records.
        path.unlink()
        require_execution_policy(None, None)
        with pytest.raises(ValueError, match='binding_unconfirmed'):
            resolve_execution_binding({'id': 'new'})


def test_running_original_process_defers_required_policy_preparation(tmp_path):
    path = tmp_path / 'cron' / 'jobs.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'jobs': [{'id': 'one', 'enabled': True}]}))
    before = path.read_bytes()
    checkout = Path(__file__).resolve().parents[2]
    child = subprocess.Popen([sys.executable, '-c',
        "import sys; from cron import jobs; scope=jobs.use_cron_store(sys.argv[1]); scope.__enter__(); "
        "fence=jobs._fire_job_lock('one'); held=fence.__enter__(); "
        "print('locked' if held else 'failed',flush=True); sys.stdin.read(); "
        "fence.__exit__(None,None,None); scope.__exit__(None,None,None)", str(tmp_path)],
        cwd=checkout, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with jobs.use_cron_store(tmp_path):
            with pytest.raises(ValueError, match='busy'):
                require_execution_policy(hashlib.sha256(before).hexdigest(), None)
        assert path.read_bytes() == before
        assert not (path.parent / 'execution-policy-required.json').exists()
        assert not (path.parent / 'execution-bindings.json').exists()
    finally:
        child.communicate(timeout=10)


def test_private_policy_reads_refuse_missing_posix_owner_api_and_allow_windows(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cron import execution_bindings

    path = tmp_path / "policy.json"
    path.write_bytes(b'{"schemaVersion":1}')
    path.chmod(0o600)
    monkeypatch.setattr(execution_bindings, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="storage_unconfirmed"):
        execution_bindings._private_read(path)
    monkeypatch.setattr(execution_bindings, "os", SimpleNamespace(name="nt"))
    assert execution_bindings._private_read(path) == path.read_bytes()
