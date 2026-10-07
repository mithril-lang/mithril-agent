"""Real CLI requests, original script execution and retained profile-local results."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.platforms('any')
CHECKOUT = Path(__file__).resolve().parents[2]


def setup(home, script_body=None):
    scripts = home / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / 'effect.py'
    script.write_text(script_body or (
        "from pathlib import Path\n"
        "p = Path('effects.txt')\n"
        "with p.open('a') as f: f.write('effect\\n')\n"
        "print('original script completed')\n"))
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_TIMEZONE': 'UTC'}
    create = subprocess.run([sys.executable, '-c',
        "import json,sys; from cron.jobs import create_job; "
        "print(json.dumps(create_job(prompt=None, schedule='2h', script=sys.argv[1], "
        "workdir=sys.argv[2], no_agent=True, deliver='local')))" , str(script), str(home)],
        cwd=CHECKOUT, env=env, capture_output=True, text=True, timeout=30)
    assert create.returncode == 0, create.stderr
    job = json.loads(create.stdout)
    path = home / 'cron' / 'jobs.json'
    request = {'owner': 'alice', 'profile': 'default', 'operationId': 'run-original',
               'jobId': job['id'], 'expectedVersion': hashlib.sha256(path.read_bytes()).hexdigest()}
    return env, path, request


def start(env, request):
    process = subprocess.Popen([sys.executable, str(CHECKOUT / 'hermes'), 'cron', 'source-run'],
        cwd=CHECKOUT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    process.stdin.write(json.dumps(request))
    process.stdin.close()
    process.stdin = None
    return process


def finish(process):
    stdout, stderr = process.communicate(timeout=45)
    assert process.returncode == 0, (stdout, stderr)
    response = json.loads(stdout)
    assert response['success'] is True
    return response['receipt']


def test_real_manual_runs_replay_and_profile_isolation(tmp_path):
    for index, name in enumerate(['a', 'b', 'a']):
        home = tmp_path / name
        env, path, request = setup(home)
        request['operationId'] += str(index)
        before = json.loads(path.read_text())['jobs'][-1]['next_run_at']
        receipt = finish(start(env, request))
        assert receipt == {**request, 'status': 'completed'}
        outputs = list((home / 'cron' / 'output' / request['jobId']).glob('*.md'))
        assert outputs and any('original script completed' in item.read_text() for item in outputs)
        effects = (home / 'effects.txt').read_text().splitlines()
        assert len(effects) == (2 if index == 2 else 1)
        assert json.loads(path.read_text())['jobs'][-1]['next_run_at'] is not None
        # The scheduled occurrence remains future and was not stamped completed.
        import sqlite3
        with sqlite3.connect(home / 'cron' / 'executions.db') as conn:
            assert conn.execute('SELECT scheduled_instant FROM executions').fetchall() == [(None,)] * len(effects)
        assert before is not None
        after = path.read_bytes()
        assert finish(start(env, request)) == receipt  # file version changed during original run
        assert path.read_bytes() == after
        assert (home / 'effects.txt').read_text().splitlines() == effects


def test_stale_source_and_operation_reuse_never_execute(tmp_path):
    env, path, request = setup(tmp_path / 'home')
    path.write_bytes(path.read_bytes() + b'\n')  # another client changed the full source
    before = path.read_bytes()
    stale = request
    assert finish(start(env, stale)) == {**stale, 'status': 'rejected'}
    assert path.read_bytes() == before
    assert not (path.parent.parent / 'effects.txt').exists()
    reused = start(env, {**request, 'expectedVersion': hashlib.sha256(before).hexdigest()})
    stdout, _ = reused.communicate(timeout=30)
    assert reused.returncode == 1
    assert json.loads(stdout) == {'success': False, 'error': 'operation'}
    assert path.read_bytes() == before


def test_foreign_profile_and_unexpected_fields_refuse_before_execution(tmp_path):
    env, path, request = setup(tmp_path / 'home')
    before = path.read_bytes()
    for changed, code in [({'profile': 'other'}, 'identity'),
                          ({'executor': 'web'}, 'operation'),
                          ({'expectedVersion': None}, 'operation')]:
        process = start(env, {**request, **changed})
        stdout, _ = process.communicate(timeout=30)
        assert process.returncode == 1
        assert json.loads(stdout) == {'success': False, 'error': code}
        assert path.read_bytes() == before
        assert not (path.parent.parent / 'effects.txt').exists()


def test_concurrent_request_and_killed_caller_do_not_repeat_effect(tmp_path):
    home = tmp_path / 'home'
    env, path, request = setup(home,
        "from pathlib import Path\nimport time\n"
        "with Path('effects.txt').open('a') as f: f.write('effect\\n')\n"
        "time.sleep(3)\nprint('finished')\n")
    process = start(env, request)
    try:
        deadline = time.monotonic() + 30
        while not (home / 'effects.txt').exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert finish(start(env, request)) == {**request, 'status': 'unknown'}
        process.kill()
        process.communicate(timeout=15)
        assert finish(start(env, request)) == {**request, 'status': 'unknown'}
        assert (home / 'effects.txt').read_text() == 'effect\n'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
