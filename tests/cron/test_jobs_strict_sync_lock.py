"""Cloud restoration must never use Cron's degraded best-effort write lock."""
import subprocess
import sys
import hashlib

import pytest

from cron import jobs


def test_versioned_manual_claim_cannot_degrade_its_real_store_lock(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt='original', schedule='2h')
        path = tmp_path / 'cron' / 'jobs.json'
        before = path.read_bytes()
        version = hashlib.sha256(before).hexdigest()
        monkeypatch.setattr(jobs, 'fcntl', None)
        monkeypatch.setattr(jobs, 'msvcrt', None)
        # The existing fire fence refuses first when no lock backend exists.
        assert jobs.claim_job_for_fire(job['id'], manual=True, expected_version=version) is False
        assert path.read_bytes() == before
        assert 'fire_claim' not in jobs.get_job(job['id'])


def test_sync_requires_crossprocess_lock_but_normal_cron_retains_degraded_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "fcntl", None)
    monkeypatch.setattr(jobs, "msvcrt", None)
    with jobs.use_cron_store(tmp_path):
        with jobs._jobs_lock():
            with pytest.raises(RuntimeError, match="requires the jobs file lock"):
                with jobs._jobs_lock(require_cross_process=True):
                    pytest.fail("sync entered a degraded nested lock")
        with pytest.raises(RuntimeError, match="requires the jobs file lock"):
            with jobs._jobs_lock(require_cross_process=True):
                pytest.fail("sync entered without a lock backend")
        with jobs._jobs_lock():
            jobs.save_jobs([])
    assert (tmp_path / "cron" / "jobs.json").exists()


def test_sync_reuses_successful_original_lock_and_clears_ownership_on_exit(tmp_path):
    if jobs.fcntl is None and jobs.msvcrt is None:
        pytest.skip("platform has no cross-process lock backend")
    for home in [tmp_path / "a", tmp_path / "b", tmp_path / "a"]:
        with jobs.use_cron_store(home):
            with jobs._jobs_lock(require_cross_process=True):
                with jobs._jobs_lock(require_cross_process=True):
                    jobs.save_jobs([])
            assert not jobs._jobs_lock_state.cross_process
            assert jobs._jobs_lock_state.depth == 0
            assert (home / "cron" / "jobs.json").exists()


def test_sync_cannot_reuse_another_profiles_lock(tmp_path):
    if jobs.fcntl is None and jobs.msvcrt is None:
        pytest.skip("platform has no cross-process lock backend")
    with jobs.use_cron_store(tmp_path / "a"):
        with jobs._jobs_lock(require_cross_process=True):
            with jobs.use_cron_store(tmp_path / "b"):
                with pytest.raises(RuntimeError, match="requires the jobs file lock"):
                    with jobs._jobs_lock(require_cross_process=True):
                        jobs.save_jobs([])
            # The original profile still owns its lock after the refused B write.
            with jobs._jobs_lock(require_cross_process=True):
                jobs.save_jobs([])
    assert not (tmp_path / "b" / "cron" / "jobs.json").exists()
    assert (tmp_path / "a" / "cron" / "jobs.json").exists()
    assert jobs._jobs_lock_state.store is None


def test_sync_refuses_a_lock_held_by_another_real_process(tmp_path, monkeypatch):
    if jobs.fcntl is None:
        pytest.skip("POSIX cross-process flock fixture")
    cron = tmp_path / "cron"
    cron.mkdir()
    child = subprocess.Popen([sys.executable, "-c",
        "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); "
        "print('locked',flush=True); sys.stdin.read()", str(cron / ".jobs.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.2)
        with jobs.use_cron_store(tmp_path):
            with pytest.raises(RuntimeError, match="requires the jobs file lock"):
                with jobs._jobs_lock(require_cross_process=True):
                    jobs.save_jobs([])
        assert not (cron / "jobs.json").exists()
    finally:
        child.communicate(timeout=10)
    with jobs.use_cron_store(tmp_path):
        with jobs._jobs_lock(require_cross_process=True):
            jobs.save_jobs([])
    assert (cron / "jobs.json").exists()
