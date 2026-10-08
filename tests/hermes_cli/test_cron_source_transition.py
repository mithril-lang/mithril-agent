"""The actual profile-scoped CLI prepares lifecycle changes without saving source data."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms('any')


def test_transition_cli_retains_exact_source_provenance_and_refuses_other_scopes(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    for name in ['a', 'b', 'a']:
        home = tmp_path / name
        path = home / 'cron' / 'jobs.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        source = {'id': 'abcdef123456', 'name': 'Original', 'prompt': '日本語',
                  'schedule': {'kind': 'interval', 'minutes': 120}, 'deliver': 'local',
                  'enabled': False, 'state': 'paused', 'future_metadata': {'home': name},
                  'next_run_at': '2000-01-01T00:00:00+09:00'}
        path.write_text(json.dumps({'jobs': [source], 'header': name}), encoding='utf-8')
        before = path.read_bytes()
        env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_TIMEZONE': 'Asia/Tokyo',
               'PYTHONIOENCODING': 'ascii:backslashreplace'}
        request = {'owner': 'alice', 'profile': 'default', 'operationId': 'transition-'+name,
                   'timeZone': 'Asia/Tokyo', 'action': 'resume', 'source': source}

        def call(value):
            return subprocess.run([sys.executable, str(checkout / 'hermes'), 'cron', 'source-transition'],
                input=json.dumps(value, ensure_ascii=False), encoding='utf-8', capture_output=True,
                env=env, cwd=checkout, timeout=30)

        response = call(request)
        assert response.returncode == 0, response.stderr
        prepared = json.loads(response.stdout)['preparation']
        assert {key: prepared[key] for key in request} == request
        assert prepared['job']['enabled'] is True
        assert prepared['job']['state'] == 'scheduled'
        assert prepared['job']['next_run_at'] == source['next_run_at']
        assert prepared['job']['future_metadata'] == source['future_metadata']
        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == ['jobs.json']
        for changed, error in [({'profile': 'other'}, 'identity'), ({'timeZone': 'UTC'}, 'timezone'),
                               ({'action': 'trigger'}, 'operation'), ({'extra': True}, 'operation'),
                               ({'source': {**source, 'run_claim': {'owner': 'retain'}}}, 'busy')]:
            refused = call({**request, **changed})
            assert refused.returncode == 1
            assert json.loads(refused.stdout) == {'success': False, 'error': error}
            assert path.read_bytes() == before
