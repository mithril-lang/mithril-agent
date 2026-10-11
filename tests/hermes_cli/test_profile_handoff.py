"""Actual profile files, SQLite history and cross-process admission during handoffs."""
import base64
import hashlib
import io
import select
import subprocess
import sys
from pathlib import Path
import tarfile
import uuid

import pytest

from hermes_cli import profile_handoff as handoff
from hermes_state import SessionDB

KEY = "ab" * 32


def profile(root):
    home = root / "profiles" / "handoff-trial-roundtrip"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("# keep this comment\nmodel: trial-model\n")
    (home / "profile.yaml").write_text("display_name: Trial Profile\n")
    (home / "SOUL.md").write_text("Remember the roundtrip marker.")
    (home / ".env").write_text("CUSTOM_PROVIDER_MITHRIL_KEY=trial-only-placeholder\n")
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("canonical", source="desktop")
        db.set_session_title("canonical", "Bot Chat")
        db.append_message("canonical", "user", "LOCAL_MARKER")
    handoff.enroll(home)
    return home


def move(source, destination, target):
    op = uuid.uuid4().hex
    handoff.freeze(source, op, target)
    capsule = handoff.export(source, op, KEY)
    assert capsule == handoff.export(source, op, KEY), "retry must carry the identical archive"
    handoff.stage(destination, capsule, target, KEY)
    with pytest.raises(handoff.HandoffError):
        handoff.activate(destination, op, "not-released")
    proof = handoff.release(source, op, capsule["sha256"])
    assert proof == handoff.release(source, op, capsule["sha256"])
    handoff.activate(destination, op, proof)
    handoff.activate(destination, op, proof)
    return capsule, proof


def test_roundtrip_preserves_config_credentials_canonical_chat_and_latest_history(tmp_path):
    local = profile(tmp_path / "local")
    remote = tmp_path / "spaces" / "profiles" / local.name
    remote.parent.mkdir(parents=True)
    original = {name: (local / name).read_bytes() for name in ("config.yaml", "profile.yaml", "SOUL.md", ".env")}
    old_capsule, old_proof = move(local, remote, uuid.uuid4().hex)
    with pytest.raises(handoff.HandoffError), handoff.execution(local):
        pass
    with handoff.execution(remote), SessionDB(db_path=remote / "state.db") as db:
        assert db.get_session_by_title("Bot Chat")["id"] == "canonical"
        db.append_message("canonical", "assistant", "SPACES_MARKER")
    move(remote, local, uuid.uuid4().hex)
    for name, body in original.items():
        assert (local / name).read_bytes() == body
    with handoff.execution(local), SessionDB(db_path=local / "state.db") as db:
        assert [row["content"] for row in db.get_messages("canonical")] == ["LOCAL_MARKER", "SPACES_MARKER"]
        assert db.get_session_by_title("Bot Chat")["id"] == "canonical"
    assert handoff.status(local)["generation"] == 2
    with pytest.raises(handoff.HandoffError), handoff.execution(remote):
        pass
    # A stale activation/export cannot revive the now relinquished Spaces copy.
    with pytest.raises(handoff.HandoffError):
        handoff.activate(remote, old_capsule["operation"], old_proof)
    with pytest.raises(handoff.HandoffError):
        handoff.stage(remote, old_capsule, old_capsule["target"], KEY)


def test_active_turn_in_another_process_prevents_freeze(tmp_path):
    home = profile(tmp_path)
    # A small independent interpreter avoids importing pytest/SessionDB in a
    # multiprocessing spawn child just to hold a file lease under a loaded CI.
    script = "from pathlib import Path; import sys; from hermes_cli.profile_handoff import execution\nwith execution(Path(sys.argv[1])):\n print('ready', flush=True)\n sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-c", script, str(home)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 30)[0]
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(handoff.HandoffError, match="busy"):
            handoff.freeze(home, uuid.uuid4().hex, uuid.uuid4().hex)
        assert handoff.status(home)["phase"] == "active"
    finally:
        try:
            child.communicate("finish\n", timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.communicate(timeout=10)
    assert child.returncode == 0


def test_failed_or_uncertain_transfer_never_reenables_source(tmp_path):
    source = profile(tmp_path / "local")
    op, target = uuid.uuid4().hex, uuid.uuid4().hex
    handoff.freeze(source, op, target)
    capsule = handoff.export(source, op, KEY)
    destination = tmp_path / "remote" / source.name
    destination.parent.mkdir()
    damaged = {**capsule, "archive": base64.b64encode(b"damaged").decode()}
    with pytest.raises(handoff.HandoffError):
        handoff.stage(destination, damaged, target, KEY)
    assert not destination.exists()
    assert handoff.status(source)["phase"] == "frozen"
    handoff.stage(destination, capsule, target, KEY)
    # Simulated restart: fresh calls must observe the durable staged/frozen fence.
    for home in (source, destination):
        with pytest.raises(handoff.HandoffError), handoff.execution(home):
            pass
    proof = handoff.release(source, op, capsule["sha256"])
    assert handoff.status(source)["phase"] == "moved"
    handoff.stage(destination, capsule, target, KEY)
    handoff.activate(destination, op, proof)
    with handoff.execution(destination):
        pass


def test_destination_collision_wrong_gateway_and_unsafe_archive_rejected(tmp_path):
    source = profile(tmp_path / "local")
    destination = profile(tmp_path / "remote")
    op, target = uuid.uuid4().hex, uuid.uuid4().hex
    handoff.freeze(source, op, target)
    capsule = handoff.export(source, op, KEY)
    with pytest.raises(handoff.HandoffError, match="another gateway"):
        handoff.stage(destination, capsule, uuid.uuid4().hex, KEY)
    with pytest.raises(handoff.HandoffError, match="already exists"):
        handoff.stage(destination, capsule, target, KEY)
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    unsafe = {**capsule, "profile_name": "handoff-trial-fresh", "sha256": hashlib.sha256(body.getvalue()).hexdigest()}
    nonce = hashlib.sha256(op.encode()).digest()[:12]
    unsafe["archive"] = base64.b64encode(handoff._cipher(KEY).encrypt(nonce, body.getvalue(), handoff._aad(unsafe))).decode()
    fresh = destination.parent / "handoff-trial-fresh"
    with pytest.raises(handoff.HandoffError, match="Unsafe"):
        handoff.stage(fresh, unsafe, target, KEY)
    assert not fresh.exists()
    with pytest.raises(handoff.HandoffError, match="another profile name"):
        handoff.stage(fresh, capsule, target, KEY)


def test_existing_bots_and_messaging_profiles_cannot_be_enrolled(tmp_path):
    bot = tmp_path / "existing-bot"
    bot.mkdir()
    with pytest.raises(handoff.HandoffError, match="Only"):
        handoff.enroll(bot)
    with handoff.execution(bot):
        pass
    trial = tmp_path / "handoff-trial-messaging"
    trial.mkdir()
    (trial / ".env").write_text("TELEGRAM_BOT_TOKEN=existing-secret-placeholder\n")
    with pytest.raises(handoff.HandoffError, match="Messaging"):
        handoff.enroll(trial)


def test_restart_enumeration_excludes_frozen_staged_and_moved_profile_and_failed_drain_cannot_export(tmp_path):
    from hermes_cli.profiles import profile_is_parked
    home = profile(tmp_path)
    operation, target = uuid.uuid4().hex, uuid.uuid4().hex
    def drain_failed():
        raise handoff.HandoffError("Gateway did not confirm retiring the old runtime")
    with pytest.raises(handoff.HandoffError):
        handoff.freeze(home, operation, target, quiesce=drain_failed)
    assert profile_is_parked(home)
    with pytest.raises(handoff.HandoffError):
        handoff.export(home, operation, KEY)
    handoff.freeze(home, operation, target, quiesce=lambda: None)
    capsule = handoff.export(home, operation, KEY)
    destination = tmp_path / "other" / home.name
    destination.parent.mkdir()
    handoff.stage(destination, capsule, target, KEY)
    assert profile_is_parked(destination)
    proof = handoff.release(home, operation, capsule["sha256"])
    assert profile_is_parked(home)
    handoff.activate(destination, operation, proof)
    assert not profile_is_parked(destination)


def test_cached_agent_cannot_resume_an_earlier_generation_after_roundtrip(tmp_path):
    source = profile(tmp_path / "local")
    class CachedAgent:
        @handoff.execution_turn
        def run_conversation(self):
            return "ran"
    cached = CachedAgent()
    cached.logs_dir = source / "sessions"
    before = handoff.status(source)
    cached._execution_handoff_owner = (before["identity"], before["generation"])
    assert cached.run_conversation() == "ran"
    remote = tmp_path / "remote" / source.name
    remote.parent.mkdir()
    move(source, remote, uuid.uuid4().hex)
    move(remote, source, uuid.uuid4().hex)
    with pytest.raises(handoff.HandoffError, match="earlier profile generation"):
        cached.run_conversation()


def test_serve_scheduler_footprint_is_eligible_but_real_jobs_are_rejected(tmp_path):
    home = tmp_path / "handoff-trial-scheduler"
    (home / "cron").mkdir(parents=True)
    for name in (".jobs.lock", ".tick.lock", "ticker_heartbeat", "ticker_last_success"):
        (home / "cron" / name).write_text("fixture")
    jobs = home / "cron" / "jobs.json"
    jobs.write_text('{"jobs": []}')
    import sqlite3
    with sqlite3.connect(home / "cron" / "executions.db") as db:
        db.execute("CREATE TABLE executions (id TEXT PRIMARY KEY)")
    handoff.enroll(home)
    jobs.write_text('{"jobs": [{"id": "real-job"}]}')
    with pytest.raises(handoff.HandoffError, match="scheduled"):
        handoff.freeze(home, uuid.uuid4().hex, uuid.uuid4().hex)
    assert handoff.status(home)["phase"] == "active"
    jobs.write_text('{"jobs": []}')
    with sqlite3.connect(home / "cron" / "executions.db") as db:
        db.execute("INSERT INTO executions VALUES ('past-execution')")
    with pytest.raises(handoff.HandoffError, match="scheduled"):
        handoff.freeze(home, uuid.uuid4().hex, uuid.uuid4().hex)
