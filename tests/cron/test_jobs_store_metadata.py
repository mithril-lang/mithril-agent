"""Original schedule store metadata survives real writer operations per profile."""
import json

from cron.jobs import load_jobs, save_jobs, use_cron_store


def test_original_store_metadata_survives_scoped_a_b_a_writes(tmp_path):
    homes = [tmp_path / "a", tmp_path / "b"]
    for index, home in enumerate(homes):
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({
            "jobs": [], "schema_extension": {"profile": index, "nested": [None, True, "retain"]},
            "original_version": index + 1, "updated_at": "old",
        }), encoding="utf-8")
    for home in [homes[0], homes[1], homes[0]]:
        path = home / "cron" / "jobs.json"
        before = json.loads(path.read_text(encoding="utf-8"))
        with use_cron_store(home):
            save_jobs(load_jobs())
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["schema_extension"] == before["schema_extension"]
        assert after["original_version"] == before["original_version"]
        assert after["jobs"] == before["jobs"]
        assert after["updated_at"] != "old"


def test_intentional_removal_preserves_unrelated_store_metadata(tmp_path):
    home = tmp_path / "profile"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    # Disabled records cannot dispatch; this exercises the real shrink guard.
    path.write_text(json.dumps({"jobs": [{"id": "removed", "enabled": False,
        "state": "paused", "name": "Original", "prompt": "Keep", "schedule": {
            "kind": "interval", "minutes": 30}, "next_run_at": None}],
        "original_metadata": {"keep": "exact"}}), encoding="utf-8")
    with use_cron_store(home):
        save_jobs([], removed_ids={"removed"})
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["jobs"] == []
    assert after["original_metadata"] == {"keep": "exact"}


def test_explicit_corrupt_repair_and_initial_save_still_create_valid_stores(tmp_path):
    for content in [None, "{broken", b"\xff"]:
        home = tmp_path / str(len(list(tmp_path.iterdir())))
        (home / "cron").mkdir(parents=True)
        path = home / "cron" / "jobs.json"
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif content is not None:
            path.write_text(content, encoding="utf-8")
        with use_cron_store(home):
            save_jobs([], replace=True)
        assert json.loads(path.read_text(encoding="utf-8"))["jobs"] == []
