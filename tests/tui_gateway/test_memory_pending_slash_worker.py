"""Real persistent slash workers review profile-local pending memory without inference.

This qualifies the backend used by Desktop slash.exec, not a rendered human UI
or Mithril Web's restricted relay. Workers stay alive while proposals and routes
change, so a one-shot fresh CLI cannot conceal stale profile/store state.
"""

import copy
import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("action", ["add", "replace", "remove", "batch"])
@pytest.mark.platforms("posix")
def test_persistent_slash_workers_keep_pending_memory_custody(tmp_path, monkeypatch, target, action):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from tools.registry import registry
    from tui_gateway.server import _SlashWorker, _session_profile_runtime_scope

    # No real account or provider endpoint is needed for human memory commands.
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in list(os.environ):
        if key.endswith(("_API_KEY", "_TOKEN")):
            monkeypatch.delenv(key)
    sessions, workers, frozen, stores = {}, {}, {}, {}
    filename = "USER.md" if target == "user" else "MEMORY.md"
    try:
        for owner in ("a", "b"):
            home = tmp_path / owner
            home.mkdir()
            (home / "config.yaml").write_text(json.dumps({
                "memory": {"write_approval": True},
                "model": {"default": "qualification-no-inference", "provider": "custom",
                          "base_url": "http://127.0.0.1:9/v1"},
            }))
            original, replacement = home / "original", home / "replacement"
            original.mkdir()
            replacement.mkdir()
            (home / "memories").symlink_to(original, target_is_directory=True)
            sessions[owner] = {"profile_home": str(home)}
            with _session_profile_runtime_scope(sessions[owner], hydrate_secrets=False):
                store = MemoryStore()
                assert store.add(target, f"initial-{owner}")["success"]
                assert store.add(target, f"spare-{owner}")["success"]
                store.load_from_disk()
                stores[owner] = store
                frozen[owner] = copy.deepcopy(store._system_prompt_snapshot)
                workers[owner] = _SlashWorker("same-durable-owner", "qualification-no-inference",
                                              profile_home=str(home), provider="custom")
            assert "No pending memory writes" in workers[owner].run("/memory pending")

        expected = {owner: [f"initial-{owner}", f"spare-{owner}"] for owner in sessions}
        for visit, owner in enumerate(("a", "b", "a")):
            home = Path(sessions[owner]["profile_home"])
            other = "b" if owner == "a" else "a"
            content = f"reviewed-{owner}-{visit}: " + "reviewed detail " * 12 + f"\n全文の末尾-{owner}-{visit}"
            args = {"action": action, "target": target, "content": content}
            next_entries = list(expected[owner])
            if action == "add":
                next_entries.append(content)
            elif action == "replace":
                args["old_text"] = next_entries[0]
                next_entries[0] = content
            elif action == "remove":
                args["old_text"] = next_entries.pop(0)
                args.pop("content")
            else:
                args = {"target": target, "operations": [
                    {"action": "replace", "old_text": next_entries[0], "content": None, "new_text": content},
                    {"action": "add", "content": f"batch-extra-{owner}-{visit}"},
                ]}
                next_entries[0] = content
                next_entries.append(f"batch-extra-{owner}-{visit}")
            with _session_profile_runtime_scope(sessions[owner], hydrate_secrets=False):
                result = json.loads(registry.dispatch("memory", args, store=load_on_disk_store()))
                assert result["success"] and result["staged"], result
                pending_id = result["pending_id"]
                record = wa.get_pending(wa.MEMORY, pending_id)
                assert load_on_disk_store()._entries_for(target) == expected[owner]
            listing = workers[owner].run("/memory pending")
            assert pending_id in listing
            assert filename in listing
            if action != "remove":
                assert content.splitlines()[-1] in listing
                assert json.dumps(content, ensure_ascii=False) in listing
            if action == "batch":
                assert f"batch-extra-{owner}-{visit}" in listing
            if action in {"replace", "remove", "batch"}:
                assert expected[owner][0] in listing
            assert pending_id not in workers[other].run("/memory pending")
            assert "No pending memory writes" in workers[other].run(f"/memory approve {pending_id}")

            # Identical bytes at a different path still cannot inherit the review.
            before = (home / "original" / filename).read_bytes()
            (home / "replacement" / filename).write_bytes(before)
            (home / "memories").unlink()
            (home / "memories").symlink_to(home / "replacement", target_is_directory=True)
            refused = workers[owner].run(f"/memory approve {pending_id}")
            assert "Approved 0" in refused and "store changed" in refused, refused
            with _session_profile_runtime_scope(sessions[owner], hydrate_secrets=False):
                assert wa.get_pending(wa.MEMORY, pending_id) == record
            assert (home / "replacement" / filename).read_bytes() == before
            assert (home / "original" / filename).read_bytes() == before
            (home / "memories").unlink()
            (home / "memories").symlink_to(home / "original", target_is_directory=True)
            approved = workers[owner].run(f"/memory approve {pending_id}")
            assert "Approved 1" in approved, approved
            assert "No pending memory writes" in workers[owner].run(f"/memory approve {pending_id}")
            expected[owner] = next_entries
            for key, session in sessions.items():
                with _session_profile_runtime_scope(session, hydrate_secrets=False):
                    assert load_on_disk_store()._entries_for(target) == expected[key]
                    assert wa.list_pending(wa.MEMORY) == []
                assert stores[key]._system_prompt_snapshot == frozen[key]

        # Rejection discards only the selected profile's proposal, without applying it.
        for owner, session in sessions.items():
            with _session_profile_runtime_scope(session, hydrate_secrets=False):
                result = json.loads(registry.dispatch("memory", {
                    "action": "add", "target": target, "content": "must remain rejected",
                }, store=stores[owner]))
                assert result["success"] and result["staged"]
            rejected = workers[owner].run(f"/memory reject {result['pending_id']}")
            assert "Rejected pending memory write" in rejected, rejected
            with _session_profile_runtime_scope(session, hydrate_secrets=False):
                assert load_on_disk_store()._entries_for(target) == expected[owner]
                assert wa.list_pending(wa.MEMORY) == []
    finally:
        for worker in workers.values():
            worker.close()
            assert worker.proc.poll() is not None
