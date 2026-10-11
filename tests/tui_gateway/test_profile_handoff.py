"""Exercise authenticated RPC dispatch with real scoped stores and durable ownership."""
import uuid
from pathlib import Path

import pytest
import tui_gateway.server as server
from hermes_cli import profile_handoff as handoff


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "gateway"
    trial = root / "profiles" / "handoff-trial-rpc"
    trial.mkdir(parents=True)
    (trial / "config.yaml").write_text("display:\n  busy_input_mode: queue\n")
    (trial / ".env").write_text("CUSTOM_PROVIDER_MITHRIL_KEY=secondary-fixture\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(server, "_hermes_home", str(root))
    monkeypatch.setattr(server, "_sessions", {})
    return root, trial


def call(method, params):
    return server.handle_request({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params})


def test_enroll_freeze_rpc_blocks_prompt_session_creation_and_config_writes(homes):
    root, trial = homes
    before = (trial / "config.yaml").read_bytes()
    response = call("profiles.handoff", {"action": "enroll", "name": trial.name})
    assert response["result"]["phase"] == "active"
    assert (root / ".execution-handoff-locks").is_dir()
    assert not (root.parent / ".execution-handoff-locks").exists(), "cloud gateway must not require writing /opt"
    operation, target = uuid.uuid4().hex, uuid.uuid4().hex
    response = call("profiles.handoff", {"action": "freeze", "name": trial.name, "operation": operation, "target": target})
    assert response["result"]["phase"] == "frozen"
    for method, params in [
        ("session.create", {"profile": trial.name}),
        ("config.set", {"profile": trial.name, "key": "busy", "value": "steer"}),
        ("profiles.configure", {"name": trial.name, "soul": "unwanted write"}),
    ]:
        assert call(method, params)["error"]["code"] == 4068
    assert (trial / "config.yaml").read_bytes() == before
    exported = call("profiles.handoff", {"action": "export", "name": trial.name, "operation": operation, "encryption_key": "ab" * 32})["result"]["capsule"]
    assert "secondary-fixture" not in exported["archive"]
    released = call("profiles.handoff", {"action": "release", "name": trial.name, "operation": operation, "sha256": exported["sha256"]})
    assert released["result"]["proof"]
    assert call("profiles.handoff", {"action": "status", "name": trial.name})["result"]["phase"] == "moved"
    assert not (root / "config.yaml").exists(), "management cannot rewrite the launch profile"


def test_enrollment_rejects_existing_live_sessions_and_leaves_legacy_bot_untouched(homes):
    root, trial = homes
    server._sessions["live"] = {"profile_home": str(trial), "running": False}
    response = call("profiles.handoff", {"action": "enroll", "name": trial.name})
    assert response["error"]["code"] == 4068
    assert handoff.status(trial)["phase"] == "unmanaged"
    ordinary = root / "profiles" / "existing-bot"
    ordinary.mkdir()
    (ordinary / "config.yaml").write_text("model: existing\n")
    response = call("profiles.handoff", {"action": "enroll", "name": ordinary.name})
    assert response["error"]["code"] == 4068
    with handoff.execution(ordinary):
        assert (ordinary / "config.yaml").read_text() == "model: existing\n"


def test_shared_profile_export_and_import_never_fork_execution_ownership(homes, tmp_path):
    import io
    import tarfile
    from hermes_cli.profiles import export_profile, import_profile
    root, trial = homes
    handoff.enroll(trial)
    archive = export_profile(trial.name, str(tmp_path / "shared"))
    with tarfile.open(archive) as saved:
        assert not any(name.endswith(handoff.STATE) for name in saved.getnames())
    # Even a foreign archive containing an owner journal cannot install another
    # active copy of that identity through the ordinary sharing interface.
    foreign = tmp_path / "foreign.tar.gz"
    with tarfile.open(foreign, "w:gz") as saved:
        for name in ("config.yaml", handoff.STATE):
            body = (trial / name).read_bytes()
            entry = tarfile.TarInfo("handoff-trial-imported/" + name)
            entry.size = len(body)
            saved.addfile(entry, io.BytesIO(body))
    imported = import_profile(str(foreign))
    assert handoff.status(imported)["phase"] == "unmanaged"
    assert handoff.status(trial)["phase"] == "active"


def test_linked_profile_cannot_create_an_alternate_execution_lease(homes):
    root, trial = homes
    alias = trial.parent / "handoff-trial-alias"
    alias.symlink_to(trial, target_is_directory=True)
    assert call("profiles.handoff", {"action": "enroll", "name": alias.name})["error"]["code"] == 4068
    assert handoff.status(trial)["phase"] == "unmanaged"


def test_named_backend_reopens_latest_store_and_retires_only_old_profile_cache_after_roundtrip(homes, monkeypatch):
    from hermes_state_registry import acquire
    from hermes_state import SessionDB
    root, trial = homes
    with SessionDB(db_path=trial / "state.db") as db:
        db.create_session("canonical", source="desktop")
        db.append_message("canonical", "user", "before")
    handoff.enroll(trial)
    monkeypatch.setattr(server, "_hermes_home", str(trial))
    monkeypatch.setattr(server, "_handoff_runtime_owners", {})
    monkeypatch.setattr(server, "_db", acquire(trial / "state.db"))
    with server._profile_handoff_runtime(trial):
        old_db = server._get_db()
    old_session = {"profile_home": None, "running": False}
    legacy_session = {"profile_home": str(root / "profiles" / "existing-bot"), "running": False}
    server._sessions.update(old=old_session, legacy=legacy_session)
    retired = []
    monkeypatch.setattr(server, "_teardown_popped_session", lambda session, **kwargs: retired.append(session))
    remote = root / "remote" / trial.name
    remote.parent.mkdir()
    def move(source, destination):
        op, target = uuid.uuid4().hex, uuid.uuid4().hex
        handoff.freeze(source, op, target)
        capsule = handoff.export(source, op, "ab" * 32)
        handoff.stage(destination, capsule, target, "ab" * 32)
        handoff.activate(destination, op, handoff.release(source, op, capsule["sha256"]))
    move(trial, remote)
    with SessionDB(db_path=remote / "state.db") as db:
        db.append_message("canonical", "assistant", "latest remote history")
    move(remote, trial)
    with server._profile_handoff_runtime(trial):
        reopened = server._get_db()
        assert reopened is not old_db
        assert [row["content"] for row in reopened.get_messages("canonical")] == ["before", "latest remote history"]
    assert retired == [old_session]
    assert server._sessions == {"legacy": legacy_session}
    reopened.close()


def test_stage_into_fresh_gateway_creates_profiles_parent_without_activating(homes, tmp_path, monkeypatch):
    root, trial = homes
    destination = tmp_path / "fresh-destination"
    destination.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(destination))
    monkeypatch.setattr(server, "_hermes_home", str(destination))
    gateway = call("profiles.handoff", {"action": "gateway"})["result"]["gateway"]
    handoff.enroll(trial)
    op = uuid.uuid4().hex
    handoff.freeze(trial, op, gateway)
    capsule = handoff.export(trial, op, "ab" * 32)
    assert not (destination / "profiles").exists()
    staged = call("profiles.handoff", {"action": "stage", "name": trial.name, "capsule": capsule, "encryption_key": "ab" * 32})
    assert staged["result"]["phase"] == "staged"
    with pytest.raises(handoff.HandoffError), handoff.execution(destination / "profiles" / trial.name):
        pass
