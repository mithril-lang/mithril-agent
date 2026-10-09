"""Pending memory review retains the built-in store actually reviewed, across profile scopes."""

import json

import pytest


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("origin", ["foreground", "background_review"])
@pytest.mark.parametrize("redirected", [False, True])
@pytest.mark.platforms("posix")
def test_pending_approval_keeps_reviewed_store_and_profile(tmp_path, monkeypatch, target, origin, redirected):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from tools.registry import registry
    from tools.skill_provenance import set_current_write_origin, reset_current_write_origin
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope

    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = {k: {"profile_home": str(tmp_path / k)} for k in ("a", "b")}
    proposals, stores = {}, {}
    for owner, session in sessions.items():
        home = tmp_path / owner
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps({"memory": {"write_approval": True}}))
        original, replacement = home / "original", home / "replacement"
        original.mkdir(); replacement.mkdir()
        (home / "memories").symlink_to(original, target_is_directory=True)
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            store = MemoryStore()
            assert store.add(target, "reviewed entry")["success"]
            filename = store._path_for(target).name
            (replacement / filename).write_bytes((original / filename).read_bytes())
            store.load_from_disk()
            stores[owner] = (store, dict(store._system_prompt_snapshot))
            token = set_current_write_origin(origin)
            try:
                result = json.loads(registry.dispatch("memory", {"action": "remove", "target": target,
                                                               "old_text": "reviewed entry"}, store=store))
            finally:
                reset_current_write_origin(token)
            assert result["success"] and result["staged"]
            proposals[owner] = result["pending_id"]
        if redirected:
            (home / "memories").unlink()
            (home / "memories").symlink_to(replacement, target_is_directory=True)

    for owner in ("a", "b", "a"):
        session, home = sessions[owner], tmp_path / owner
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            record = wa.get_pending(wa.MEMORY, proposals[owner])
            if record is None:
                assert not redirected
                continue
            text = handle_pending_subcommand(wa.MEMORY, ["approve", proposals[owner]], memory_store=load_on_disk_store())
            if redirected:
                assert "Approved 0" in text and "store changed" in text, text
                assert wa.get_pending(wa.MEMORY, proposals[owner]) == record
            else:
                assert "Approved 1" in text, text
                assert wa.get_pending(wa.MEMORY, proposals[owner]) is None
            assert stores[owner][0]._system_prompt_snapshot == stores[owner][1]
            filename = "USER.md" if target == "user" else "MEMORY.md"
            assert "reviewed entry" in (home / "replacement" / filename).read_text()
            assert ("reviewed entry" in (home / "original" / filename).read_text()) == redirected
    if redirected:
        for owner, session in sessions.items():
            home = tmp_path / owner
            (home / "memories").unlink()
            (home / "memories").symlink_to(home / "original", target_is_directory=True)
            with _session_profile_runtime_scope(session, hydrate_secrets=False):
                text = handle_pending_subcommand(wa.MEMORY, ["approve", proposals[owner]], memory_store=load_on_disk_store())
                assert "Approved 1" in text and wa.get_pending(wa.MEMORY, proposals[owner]) is None
