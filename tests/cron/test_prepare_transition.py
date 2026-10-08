"""Read-only lifecycle preparation uses the original writer's rules and source bytes."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from cron.jobs import (create_job, pause_job, prepare_job_transition, resume_job,
                       use_cron_store)

pytestmark = pytest.mark.platforms("any")


def test_preparation_retains_original_source_and_native_resume_keeps_elapsed_slot(tmp_path):
    for name in ['a', 'b', 'a']:
        home = tmp_path / name
        with use_cron_store(home):
            created = create_job('Original', '2h', paused=True)
            path = home / 'cron' / 'jobs.json'
            file = json.loads(path.read_text(encoding='utf-8-sig'))
            source = file['jobs'][-1]
            due = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            source.update(next_run_at=due, future_metadata={'profile': name},
                          model='Original model', context_from=['prior'])
            path.write_text(json.dumps(file, ensure_ascii=False), encoding='utf-8')
            before = path.read_bytes()
            captured = copy.deepcopy(source)
            prepared = prepare_job_transition(source, 'resume')
            assert source == captured
            assert path.read_bytes() == before
            assert prepared['next_run_at'] == due
            assert prepared['future_metadata'] == captured['future_metadata']
            assert prepared['context_from'] == ['prior']
            resumed = resume_job(created['id'])
            assert resumed['next_run_at'] == due
            stored = json.loads(path.read_text(encoding='utf-8-sig'))['jobs'][-1]
            assert stored == prepared
            pause_prepared = prepare_job_transition(stored, 'pause')
            assert pause_prepared['enabled'] is False
            assert pause_prepared['state'] == 'paused'
            assert pause_prepared['future_metadata'] == captured['future_metadata']
            paused = pause_job(created['id'])
            assert {k: v for k, v in paused.items() if k != 'paused_at'} == {
                k: v for k, v in pause_prepared.items() if k != 'paused_at'}


def test_busy_terminal_or_expired_once_preparations_never_change_original_files(tmp_path):
    with use_cron_store(tmp_path / 'source'):
        job = create_job('Original', '2h', paused=True)
        path = tmp_path / 'source' / 'cron' / 'jobs.json'
        before = path.read_bytes()
        for changed, action, error in [
            ({'pending_slot': {'instant': 'retained'}}, 'pause', 'busy'),
            ({'run_claim': {'owner': 'retained'}}, 'resume', 'busy'),
            ({'fire_claim': {'owner': 'retained'}}, 'pause', 'busy'),
            ({'state': 'completed'}, 'resume', 'terminal'),
            ({}, 'trigger', 'operation'),
            ({'schedule': {'kind': 'once', 'run_at': '2000-01-01T00:00:00+00:00'},
              'next_run_at': None}, 'resume', 'Cannot resume'),
        ]:
            with pytest.raises(ValueError, match=error):
                prepare_job_transition({**job, **changed}, action)
            assert path.read_bytes() == before
