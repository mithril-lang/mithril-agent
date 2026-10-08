"""Original schedule preparation must not mutate or activate the native store."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from cron.jobs import create_job, prepare_job, use_cron_store

pytestmark = pytest.mark.platforms("any")


@pytest.mark.parametrize("plan,kind", [
    ("2h", "interval"), ("every monday 9am", "cron"),
    ("0 9 * * *", "cron"), ("in 30m", "once"),
])
def test_original_parser_prepares_complete_records_without_creating_a_store(tmp_path, plan, kind):
    home = tmp_path / "profile"
    home.mkdir()
    with use_cron_store(home):
        job = prepare_job("日本語の作業", plan, name="Original task", deliver="local",
                          skills=["kept"], context_from=["prior"], model="original-model",
                          paused=True, paused_reason="Await binding")
    assert job["schedule"]["kind"] == kind
    assert job["prompt"] == "日本語の作業"
    assert job["skills"] == ["kept"]
    assert job["context_from"] == ["prior"]
    assert job["model"] == "original-model"
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["paused_reason"] == "Await binding"
    assert list(home.iterdir()) == []
    if kind == "once":
        assert datetime.fromisoformat(job["schedule"]["run_at"]).utcoffset() is not None


def test_native_creation_and_readonly_preparation_share_the_original_record_builder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for home in [a, b]:
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [], "original_metadata": "retained"}), encoding="utf-8")
    for home in [a, b, a]:
        path = home / "cron" / "jobs.json"
        before = path.read_bytes()
        with use_cron_store(home):
            # Broad future window: scheduling behavior must not depend on narrow test timing.
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            prepared = prepare_job("  original prompt  ", future, deliver="local", paused=True)
        assert path.read_bytes() == before
        with use_cron_store(home):
            stored = create_job("  original prompt  ", future, deliver="local", paused=True)
        changed = json.loads(path.read_text(encoding="utf-8-sig"))
        assert changed["original_metadata"] == "retained"
        assert changed["jobs"][-1] == stored
        # The same parser/defaults/mode admission apply; only generated identity/time vary.
        for field in ["id", "created_at", "paused_at"]:
            prepared.pop(field)
            stored.pop(field)
        assert prepared == stored
        assert prepared["repeat"] == {"times": 1, "completed": 0}
        assert prepared["prompt"] == "original prompt"


def test_invalid_original_payloads_leave_source_bytes_and_metadata_untouched(tmp_path):
    home = tmp_path / "profile"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    path.write_text('{"jobs": [], "original_metadata": "keep"}', encoding="utf-8")
    before = path.read_bytes()
    with use_cron_store(home):
        for prompt, schedule, options in [
            ("", "30m", {}), ("task", "not-a-schedule", {}),
            ("task", "30m", {"no_agent": True}),
            ("task", "30m", {"paused_reason": "invalid without paused"}),
        ]:
            with pytest.raises(ValueError):
                prepare_job(prompt, schedule, **options)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["jobs.json"]
