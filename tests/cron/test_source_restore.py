"""Real original-store CAS/recovery, with profile switches and original writers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cron import jobs, source_restore


def fixture(home, file=None):
    path = home / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = file if file is not None else {
        "jobs": [{"id": "daily", "name": "original", "enabled": True,
                  "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "daily"},
                  "unknown_job": {"retained": True}}],
        "unknown_header": {"nested": [1, None, "retained"]},
    }
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path, value, hashlib.sha256(path.read_bytes()).hexdigest()


def restore(profile, version, file, operation="restore"):
    return source_restore.restore_original_store(owner="alice", profile=profile,
        operation_id=operation, expected_version=version, file=file)


def test_original_shape_metadata_and_lost_ack_survive_real_writers_a_b_a(tmp_path):
    stores = {profile: fixture(tmp_path / profile) for profile in ("a", "b")}
    acknowledged = {}
    for profile in ("a", "b", "a"):
        home = tmp_path / profile
        path, value, version = stores[profile]
        target = copy.deepcopy(value)
        target["jobs"][0]["name"] = "synchronized"
        with jobs.use_cron_store(home):
            if profile in acknowledged:
                later = path.read_bytes()
                assert restore(profile, version, target) == acknowledged[profile]
                assert path.read_bytes() == later
                assert json.loads(later)["jobs"][0]["name"] == "later native edit"
                continue
            receipt = restore(profile, version, target)
            acknowledged[profile] = receipt
            assert json.loads(path.read_bytes()) == target
            assert receipt["version"] == hashlib.sha256(path.read_bytes()).hexdigest()
            # The original Cron writer advances after the operation. Retrying a
            # lost receipt must neither restore stale bytes nor strip metadata.
            rows = jobs.load_jobs()
            rows[0]["name"] = "later native edit"
            jobs.save_jobs(rows)
            later = path.read_bytes()
            assert restore(profile, version, target) == receipt
            assert path.read_bytes() == later
            assert json.loads(later)["unknown_header"] == value["unknown_header"]
            changed = copy.deepcopy(target)
            changed["jobs"][0]["name"] = "another request"
            with pytest.raises(ValueError, match="operation"):
                restore(profile, version, changed)
        receipts = list(path.parent.glob(".mithril-source-*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_bytes())["before"] == json.dumps(value, ensure_ascii=False)
        if os.name != "nt":
            assert receipts[0].stat().st_mode & 0o777 == 0o600
            assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("point", ["before-write", "after-write"])
def test_real_pending_transaction_recovers_at_both_write_boundaries(tmp_path, monkeypatch, point):
    path, value, version = fixture(tmp_path)
    target = copy.deepcopy(value)
    target["jobs"][0]["prompt"] = "updated"
    original_write = source_restore._write
    failed = False

    def fail_once(destination, text):
        nonlocal failed
        if not failed and ((point == "before-write" and destination == path)
                or (point == "after-write" and destination != path and json.loads(text)["state"] == "complete")):
            failed = True
            raise OSError("simulated storage interruption")
        original_write(destination, text)

    monkeypatch.setattr(source_restore, "_write", fail_once)
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(OSError, match="interruption"):
            restore("default", version, target)
        assert json.loads(next(path.parent.glob(".mithril-source-*.json")).read_bytes())["state"] == "pending"
        receipt = restore("default", version, target)
        assert json.loads(path.read_bytes()) == target
        assert receipt["version"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert json.loads(next(path.parent.glob(".mithril-source-*.json")).read_bytes())["state"] == "complete"


def test_stale_duplicate_foreign_claims_and_unbound_activation_never_write(tmp_path):
    path, value, version = fixture(tmp_path)
    before = path.read_bytes()
    duplicate = copy.deepcopy(value)
    duplicate["jobs"].append(copy.deepcopy(duplicate["jobs"][0]))
    foreign_claim = copy.deepcopy(value)
    foreign_claim["jobs"][0]["fire_claim"] = {"by": "another-replica", "at": "2026-10-07T00:00:00Z"}
    new_job = copy.deepcopy(value)
    new_job["jobs"].append({"id": "unbound", "enabled": True})
    with jobs.use_cron_store(tmp_path):
        for candidate, expected, error in ((value, "0" * 64, "conflict"),
                (duplicate, version, "inventory"), (foreign_claim, version, "runtime"),
                (new_job, version, "ownership")):
            with pytest.raises(ValueError, match=error):
                restore("default", expected, candidate)
            assert path.read_bytes() == before
    assert not list(path.parent.glob(".mithril-source-*.json"))


def test_pending_occurrence_is_retained_and_never_marked_restored(tmp_path):
    path, value, _ = fixture(tmp_path)
    value["jobs"][0]["pending_slot"] = {"scheduled_instant": "2026-10-07T01:00:00Z"}
    path, value, version = fixture(tmp_path, value)
    before = path.read_bytes()
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(ValueError, match="busy"):
            restore("default", version, value)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".mithril-source-*.json"))


def test_utf16_source_is_not_silently_reinterpreted_as_utf8(tmp_path):
    path, _, _ = fixture(tmp_path)
    path.write_bytes(json.dumps({"jobs": []}).encode("utf-16-le"))
    before = path.read_bytes()
    version = hashlib.sha256(before).hexdigest()
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(ValueError):
            restore("default", version, {"jobs": []})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".mithril-source-*.json"))


def test_legacy_array_roundtrip_and_linked_file_refusal(tmp_path):
    path, value, version = fixture(tmp_path, [{"id": "legacy", "enabled": False, "state": "paused", "unknown": [1]}])
    with jobs.use_cron_store(tmp_path):
        restore("default", version, value)
        assert json.loads(path.read_bytes()) == value
        backup = tmp_path / "outside.json"
        path.rename(backup)
        try:
            path.symlink_to(backup)
        except (OSError, NotImplementedError):
            pytest.skip("symlink capability unavailable")
        before = backup.read_bytes()
        with pytest.raises(ValueError, match="unsafe"):
            restore("default", version, value, "linked")
        assert backup.read_bytes() == before


def test_real_firing_process_defers_restore_even_before_it_stamps_a_claim(tmp_path):
    path, value, version = fixture(tmp_path)
    before = path.read_bytes()
    target = copy.deepcopy(value)
    target["jobs"][0]["name"] = "synchronized"
    checkout = Path(__file__).resolve().parents[2]

    def firing_process():
        return subprocess.Popen([sys.executable, "-c",
            "import sys; from cron import jobs; "
            "scope=jobs.use_cron_store(sys.argv[1]); scope.__enter__(); "
            "fence=jobs._fire_job_lock('daily'); held=fence.__enter__(); "
            "print('locked' if held else 'failed',flush=True); sys.stdin.read(); "
            "fence.__exit__(None,None,None); scope.__exit__(None,None,None)", str(tmp_path)],
            cwd=checkout, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    child = firing_process()
    try:
        assert child.stdout.readline().strip() == "locked"
        with jobs.use_cron_store(tmp_path):
            with pytest.raises(ValueError, match="busy"):
                restore("default", version, target)
        assert path.read_bytes() == before
        assert not list(path.parent.glob(".mithril-source-*.json"))
    finally:
        child.communicate(timeout=10)
    with jobs.use_cron_store(tmp_path):
        receipt = restore("default", version, target)
    child = firing_process()
    try:
        assert child.stdout.readline().strip() == "locked"
        with jobs.use_cron_store(tmp_path):
            assert restore("default", version, target) == receipt
        assert json.loads(path.read_bytes()) == target
    finally:
        child.communicate(timeout=10)
