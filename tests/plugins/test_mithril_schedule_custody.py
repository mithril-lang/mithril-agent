"""Real HTTP/profile discovery and original shell-effect custody regressions."""
import importlib.util
import io
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def client():
    path = Path(__file__).resolve().parents[2] / 'plugins/mithril-schedules/client.py'
    spec = importlib.util.spec_from_file_location('schedule_custody_test_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ORIGINAL_WORKER = '''
import json, sys
from hermes_cli.plugins import discover_plugins
discover_plugins()
from cron import jobs, scheduler, executions
value = json.load(sys.stdin)
job = jobs.get_job(value['jobId'])
attempt = executions.create_execution(job['id'], source='direct', scheduled_instant=value['instant'])
job['execution_id'] = attempt['id']
job['_scheduled_instant'] = value['instant']
processed = scheduler._run_guarded_job_body(job)
print(json.dumps({'processed': processed, 'execution': executions.get_execution(attempt['id'])}))
'''


def test_durable_original_source_binding_survives_process_and_profile_restarts(tmp_path):
    import hashlib
    import stat
    from cron import jobs
    from cron.occurrences import scheduled_instant
    checkout = Path(__file__).resolve().parents[2]
    with server() as (url, calls, mode):
        for generation, name in enumerate(['a', 'b', 'a'], 1):
            home = tmp_path / 'profiles' / name
            home.mkdir(parents=True, exist_ok=True)
            (home / 'scripts').mkdir(exist_ok=True)
            effect = home / 'effect.txt'
            baseline = effect.read_text() if effect.exists() else ''
            (home / 'scripts' / 'one.sh').write_text('printf x >> "' + str(effect) + '"\necho done\n')
            (home / '.env').write_text('MITHRIL_API_KEY=mf_' + name*32 + '\n')
            (home / 'config.yaml').write_text('plugins:\n  enabled: [mithril-schedules]\n  entries:\n    mithril-schedules:\n      settings:\n        api_origin: ' + url + '\n')
            ht = set_hermes_home_override(home)
            try:
                with jobs.use_cron_store(home):
                    job = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh', no_agent=True, deliver='local')
                    # Preserve original headers, BOM, CRLF and numeric source lexemes at binding time.
                    path = home / 'cron' / 'jobs.json'
                    text = path.read_text(encoding='utf-8-sig')
                    if 'opaque' not in json.loads(text):
                        text = text.replace('{', '{\n"opaque":1.25e+0300,', 1)
                    text = '\ufeff' + text.replace('\n', '\r\n')
                    path.write_bytes(text.encode())
                    before = path.read_bytes()
                env = {'HERMES_HOME': str(home), 'MITHRIL_API_KEY': 'mf_' + name*32, 'PATH': '/usr/bin:/bin'}
                anchor = {'profile': name, 'sourceRevision': generation, 'sourceDigest': hashlib.sha256(before).hexdigest(),
                          'authorityRevision': 1, 'nativeVersion': hashlib.sha256(before).hexdigest()}
                bound = subprocess.run([sys.executable, str(checkout / 'hermes'), '-p', name, 'mithril-schedule-custody', '--stdin'],
                    input=json.dumps({'owner': 'alice', 'binding': anchor}), text=True,
                    capture_output=True, timeout=30, cwd=checkout, env=env)
                assert bound.returncode == 0, bound.stderr
                result = json.loads(bound.stdout.strip().split('\n')[-1])
                assert result['ok'], (name, result, [call[2]['action'] for call in calls])
                assert result['receipt']['nativeVersion'] == anchor['nativeVersion']
                assert path.read_bytes() == before
                for file in ['execution-bindings.json', 'execution-policy-required.json']:
                    assert stat.S_IMODE((home / 'cron' / file).stat().st_mode) == 0o600
                first = len(calls)
                # Each invocation is a fresh interpreter; counters advance without re-binding authored definitions.
                for instant in ['2026-10-07T00:17:00.000001+00:00', '2026-10-07T00:22:00.000002+00:00', None]:
                    run = subprocess.run([sys.executable, '-c', _ORIGINAL_WORKER],
                        input=json.dumps({'jobId': job['id'], 'instant': instant}), text=True,
                        capture_output=True, timeout=30, cwd=checkout, env=env)
                    assert run.returncode == 0, run.stderr
                    value = json.loads(run.stdout.strip().split('\n')[-1])
                    assert value['processed'], value
                    assert value['execution']['status'] == 'completed'
                    expected = instant or scheduled_instant(value['execution']['claimed_at'])
                    assert calls[-3][2]['scheduledInstant'] == expected
                    assert calls[-3][2]['operationId'] == value['execution']['id']
                assert effect.read_text() == baseline + 'xxx'
                assert all(c[1] == 'Bearer mf_' + name*32 and c[2]['profile'] == name for c in calls[first:])
                assert [c[2]['action'] for c in calls[first:]] == ['claim', 'transition', 'transition'] * 3
            finally:
                reset_hermes_home_override(ht)


def test_durable_required_binding_refuses_missing_store_authored_edits_and_plugin_unload(tmp_path, monkeypatch):
    import hashlib
    from cron import jobs, scheduler, executions
    from hermes_cli.plugins import PluginManager
    from cron.execution_bindings import install_execution_bindings, read_execution_binding_snapshot
    home = tmp_path / 'profiles' / 'a'
    home.mkdir(parents=True)
    (home / 'scripts').mkdir()
    effect = home / 'effect.txt'
    (home / 'scripts' / 'one.sh').write_text('printf x >> "' + str(effect) + '"\necho done\n')
    (home / '.env').write_text('MITHRIL_API_KEY=mf_' + 'a'*32 + '\n')
    with server() as (url, calls, mode):
        (home / 'config.yaml').write_text('plugins:\n  enabled: [mithril-schedules]\n  entries:\n    mithril-schedules:\n      settings:\n        api_origin: ' + url + '\n')
        ht = set_hermes_home_override(home)
        manager = PluginManager()
        try:
            manager.discover_and_load()
            monkeypatch.setattr('hermes_cli.plugins._delivery_manager', lambda: manager)
            plugin = manager._plugins['mithril-schedules'].module
            # Resolve through the actual loaded policy module (no fake execution callback).
            binding_module = sys.modules[plugin.__name__ + '.bindings'] if plugin.__name__ + '.bindings' in sys.modules else None
            if binding_module is None:
                import importlib
                binding_module = importlib.import_module(plugin.__name__ + '.bindings')
            with jobs.use_cron_store(home):
                job = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh', no_agent=True, deliver='local')
                path = home / 'cron' / 'jobs.json'
                source = path.read_bytes()
                anchor = {'profile': 'a', 'sourceRevision': 1, 'sourceDigest': 'a'*64,
                          'authorityRevision': 1, 'nativeVersion': hashlib.sha256(source).hexdigest()}
                c = client()
                binding_module.bind_original_source('alice', anchor, lambda command: c.call_custody(url, 'mf_'+'a'*32, 'alice', command))
                bindings, version = read_execution_binding_snapshot()
                # Source/store CAS prevents old producers from replacing new private state.
                for wrong_source, wrong_binding in [('b'*64, version), (anchor['nativeVersion'], 'b'*64)]:
                    try:
                        install_execution_bindings(bindings, wrong_source, wrong_binding)
                        assert False, 'stale producer changed the binding'
                    except ValueError:
                        pass
                assert path.read_bytes() == source
                first = len(calls)
                def attempt(current):
                    current = dict(current)
                    current['execution_id'] = executions.create_execution(current['id'], source='direct')['id']
                    assert not scheduler._run_guarded_job_body(current)
                    assert executions.get_execution(current['execution_id'])['delivery_outcome'] == 'suppressed'
                    assert not effect.exists()
                changed = dict(job, prompt='changed authored prompt')
                attempt(changed)
                changed = dict(job, repeat={**job['repeat'], 'times': 2})
                attempt(changed)
                changed = dict(job, opaque_author_metadata={'private': 'changed'})
                attempt(changed)
                # A new native job waits for automatic source publication/binding instead of falling back.
                new = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh', no_agent=True, deliver='local')
                attempt(new)
                store = home / 'cron' / 'execution-bindings.json'
                retained = store.read_bytes()
                store.unlink()
                attempt(job)
                store.write_bytes(retained)
                store.chmod(0o600)
                store.write_text('{invalid')
                attempt(job)
                store.write_bytes(retained)
                manager.unload()
                attempt(job)
                assert len(calls) == first
        finally:
            manager.unload()
            reset_hermes_home_override(ht)


def test_present_invalid_binding_never_runs_original_effect(tmp_path):
    from cron import jobs, scheduler, executions
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'scripts').mkdir()
    effect = home / 'effect.txt'
    (home / 'scripts' / 'one.sh').write_text('printf x >> "' + str(effect) + '"\necho done\n')
    ht = set_hermes_home_override(home)
    try:
        with jobs.use_cron_store(home):
            for binding in (None, False, '', [], {}, {'policy': ''}):
                job = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh',
                                      no_agent=True, deliver='local')
                attempt = executions.create_execution(job['id'], source='direct')
                job['execution_id'] = attempt['id']
                job['_execution_binding'] = binding
                assert not scheduler._run_guarded_job_body(job)
                assert not effect.exists()
                row = executions.get_execution(attempt['id'])
                assert row['status'] == 'failed'
                assert row['delivery_outcome'] == 'suppressed'
            # Ordinary original jobs with no binding retain their established path.
            job = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh',
                                  no_agent=True, deliver='local')
            job['execution_id'] = executions.create_execution(job['id'], source='direct')['id']
            assert scheduler._run_guarded_job_body(job)
            assert effect.read_text() == 'x'
    finally:
        reset_hermes_home_override(ht)


def test_bound_original_script_requires_fresh_start_and_records_completion(tmp_path, monkeypatch):
    import hashlib
    from cron import jobs, scheduler, executions
    from hermes_cli.plugins import PluginManager
    with server() as (url, calls, mode):
        for name, secret_char in [('a', 'a'), ('b', 'b'), ('a', 'a')]:
            home = tmp_path / name
            home.mkdir(exist_ok=True)
            (home / 'scripts').mkdir(exist_ok=True)
            effect = home / 'effect.txt'
            expected_effect = (effect.read_text() if effect.exists() else '') + 'x'
            mode['value'] = 'normal'
            (home / 'scripts' / 'one.sh').write_text('printf x >> "' + str(effect) + '"\necho done\n')
            (home / '.env').write_text('MITHRIL_API_KEY=mf_' + secret_char*32 + '\n')
            (home / 'config.yaml').write_text('plugins:\n  enabled: [mithril-schedules]\n  entries:\n    mithril-schedules:\n      settings:\n        api_origin: ' + url + '\n')
            ht = set_hermes_home_override(home)
            manager = PluginManager()
            try:
                manager.discover_and_load()
                monkeypatch.setattr('hermes_cli.plugins._delivery_manager', lambda: manager)
                with jobs.use_cron_store(home):
                    job = jobs.create_job(prompt=None, schedule='every 5m', script='one.sh', no_agent=True, deliver='local')
                    instant = '2026-10-07T00:17:00.000001+00:00'
                    attempt = executions.create_execution(job['id'], source='direct', scheduled_instant=instant)
                    job['execution_id'] = attempt['id']
                    job['_scheduled_instant'] = instant
                    fingerprint = {k: v for k, v in job.items() if k not in {'execution_id', '_scheduled_instant', 'run_claim', 'fire_claim', 'pending_slot'}}
                    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                    job['_execution_binding'] = {'policy': 'mithril-schedules', 'owner': 'alice', 'jobDigest': digest,
                        'occurrence': {'profile': 'default', 'jobId': job['id'], 'operationId': attempt['id'],
                                       'scheduledInstant': instant, 'sourceRevision': 1, 'sourceDigest': 'a'*64, 'authorityRevision': 1}}
                    first = len(calls)
                    assert scheduler._run_guarded_job_body(job)
                    assert effect.read_text() == expected_effect
                    assert [c[2]['action'] for c in calls[first:]] == ['claim', 'transition', 'transition']
                    assert calls[-1][2]['to'] == 'completed'
                    assert all(c[1] == 'Bearer mf_' + secret_char*32 for c in calls[first:])
                    assert executions.get_execution(attempt['id'])['status'] == 'completed'
                    mode['value'] = 'replayed'
                    before = len(calls)
                    assert not scheduler._run_guarded_job_body(job)
                    assert effect.read_text() == expected_effect
                    assert calls[before + 1][2]['to'] == 'running'
                    assert calls[-1][2]['to'] == 'unknown'
                    for behavior in ['retained', 'lost-start', 'foreign']:
                        mode['value'] = behavior
                        before = len(calls)
                        assert not scheduler._run_guarded_job_body(job)
                        assert effect.read_text() == expected_effect
                        if behavior == 'lost-start':
                            assert [c[2]['action'] for c in calls[before:]] == ['claim', 'transition', 'transition']
                            assert calls[-1][2]['to'] == 'unknown'
                        else:
                            assert len(calls) == before + 1
                    manager.unload()
                    before = len(calls)
                    assert not scheduler._run_guarded_job_body(job)
                    assert len(calls) == before
                    assert effect.read_text() == expected_effect
            finally:
                manager.unload()
                reset_hermes_home_override(ht)


@contextmanager
def server():
    calls = []
    mode = {'value': 'normal'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers.get('Authorization'), body))
            if mode['value'] == 'lost' or (mode['value'] == 'lost-start' and body.get('to') == 'running'):
                self.close_connection = True
                return
            if mode['value'] == 'redirect':
                self.send_response(307)
                self.send_header('Location', '/credential-leak')
                self.end_headers()
                return
            if mode['value'] == 'passive' and body['action'] == 'claim':
                self.send_response(409)
                self.end_headers()
                return
            value = {'userId': 'alice', 'profile': body['profile']}
            action = body['action']
            if action in ('status', 'select'):
                value.update(selected=mode['value'] != 'passive', revision=1)
            else:
                value['operationId'] = body['operationId']
                if action == 'claim':
                    value.update(status='admitted', fresh=mode['value'] != 'retained')
                else:
                    value['changed'] = mode['value'] != 'replayed'
            if mode['value'] == 'foreign':
                value['userId'] = 'bob'
            if mode['value'] == 'extra':
                value['private'] = 'SECRET_RESPONSE_VALUE'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}', calls, mode
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_discovery_cli_reads_the_current_profile_secret_and_checks_account_receipt(tmp_path, monkeypatch, capsys):
    from hermes_cli.plugins import PluginManager
    with server() as (url, calls, mode):
        homes = [tmp_path / 'a', tmp_path / 'b']
        for home in homes:
            home.mkdir()
            (home / 'config.yaml').write_text('plugins:\n  enabled: [mithril-schedules]\n  entries:\n    mithril-schedules:\n      settings:\n        api_origin: ' + url + '\n')
        set_multiplex_active(True)
        try:
            for home, token in [(homes[0], 'mf_' + 'a'*32), (homes[1], 'mf_' + 'b'*32), (homes[0], 'mf_' + 'a'*32)]:
                ht = set_hermes_home_override(home)
                st = set_secret_scope({'MITHRIL_API_KEY': token}, profile_home=str(home))
                manager = PluginManager()
                try:
                    manager.discover_and_load()
                    assert manager._plugins['mithril-schedules'].enabled
                    cli = next(c for c in manager._cli_commands.values() if c['name'] == 'mithril-schedule-custody')
                    envelope = {'owner': 'alice', 'command': {'action': 'status', 'profile': 'default'}}
                    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
                    cli['handler_fn'](SimpleNamespace(stdin=True))
                    output = capsys.readouterr().out.strip().split('\n')[-1]
                    assert json.loads(output)['ok']
                    assert token not in output
                    assert calls[-1][1] == 'Bearer ' + token
                    assert calls[-1][0] == '/v1/schedules/original/execution'
                finally:
                    manager.unload()
                    reset_secret_scope(st)
                    reset_hermes_home_override(ht)
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[2] / 'hermes'),
                 'mithril-schedule-custody', '--stdin'],
                input=json.dumps({'owner': 'alice', 'command': {'action': 'status', 'profile': 'default'}}),
                text=True, capture_output=True, timeout=20,
                env={'HERMES_HOME': str(homes[0]), 'MITHRIL_API_KEY': 'mf_'+'a'*32, 'PATH': '/usr/bin:/bin'})
            assert process.returncode == 0, process.stderr
            assert json.loads(process.stdout.strip().split('\n')[-1])['ok']
            assert calls[-1][1] == 'Bearer mf_'+'a'*32
        finally:
            set_multiplex_active(False)


def test_redirect_foreign_receipt_and_lost_acknowledgement_never_retry_or_admit_execution():
    c = client()
    command = {'action': 'transition', 'profile': 'default', 'jobId': 'one',
               'operationId': 'one_operation', 'scheduledInstant': '2026-10-07T00:17:00.000001+00:00',
               'authorityRevision': 1, 'sourceRevision': 1, 'sourceDigest': 'a'*64,
               'from': 'admitted', 'to': 'running'}
    with server() as (url, calls, mode):
        for behavior, error in [('redirect', 'schedule_request_failed'), ('foreign', 'schedule_receipt_unconfirmed'),
                                ('extra', 'schedule_receipt_unconfirmed'), ('lost', 'schedule_outcome_unknown')]:
            mode['value'] = behavior
            before = len(calls)
            result = c.call_custody(url, 'mf_'+'a'*32, 'alice', command)
            assert result == {'ok': False, 'error': error}
            assert len(calls) == before + 1
            assert 'SECRET_RESPONSE_VALUE' not in json.dumps(result)
        mode['value'] = 'replayed'
        assert c.call_custody(url, 'mf_'+'a'*32, 'alice', command)['receipt']['changed'] is False
        assert calls[-1][2]['scheduledInstant'] == command['scheduledInstant']


def test_only_fixed_api_or_loopback_and_bounded_exact_commands_can_send_credentials():
    c = client()
    with server() as (url, calls, mode):
        for bad_url in ['https://other.example', url+'/redirect', 'http://localhost:bad', url+'?query=1']:
            assert not c.call_custody(bad_url, 'mf_'+'a'*32, 'alice', {'action':'status','profile':'default'})['ok']
        for invalid in [{'action':'status','profile':'default','executorId':'spoof'},
                        {'action':'select','profile':'default','expectedRevision':True},
                        {'action':'status','profile':'x'*9000}]:
            assert not c.call_custody(url, 'mf_'+'a'*32, 'alice', invalid)['ok']
        assert not calls


def test_guarded_automatic_restore_keeps_original_enabled_source_without_passive_execution(tmp_path):
    import hashlib
    from cron import jobs, source_restore
    checkout = Path(__file__).resolve().parents[2]
    with server() as (url, calls, mode):
        mode['value'] = 'passive'
        for generation, name in enumerate(['a', 'b', 'a']):
            home = tmp_path / 'profiles' / name
            home.mkdir(parents=True, exist_ok=True)
            (home / 'scripts').mkdir(exist_ok=True)
            effect = home / 'effect.txt'
            (home / 'scripts' / 'one.sh').write_text('printf x >> "' + str(effect) + '"\necho done\n')
            (home / '.env').write_text('MITHRIL_API_KEY=mf_' + name*32 + '\n')
            (home / 'config.yaml').write_text('plugins:\n  enabled: [mithril-schedules]\n  entries:\n    mithril-schedules:\n      settings:\n        api_origin: ' + url + '\n')
            env = {'HERMES_HOME': str(home), 'MITHRIL_API_KEY': 'mf_' + name*32, 'PATH': '/usr/bin:/bin'}
            path = home / 'cron' / 'jobs.json'
            before = path.read_bytes() if path.exists() else None
            version = hashlib.sha256(before).hexdigest() if before is not None else None
            def cli(envelope):
                result = subprocess.run([sys.executable, str(checkout / 'hermes'), '-p', name,
                    'mithril-schedule-custody', '--stdin'], input=json.dumps(envelope), text=True,
                    capture_output=True, timeout=30, cwd=checkout, env=env)
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout.strip().split('\n')[-1])
            first = len(calls)
            prepared = cli({'owner': 'alice', 'prepare': {'profile': name, 'nativeVersion': version}})
            assert prepared['ok'], prepared
            assert (path.read_bytes() if path.exists() else None) == before
            assert len(calls) == first + 1 and calls[-1][2]['action'] == 'status'
            source = '\ufeff{\r\n"opaque":9223372036854775807,"jobs":[{"id":"synced","name":"original","enabled":true,"state":"scheduled","schedule":{"kind":"interval","seconds":300,"display":"every 5m"},"script":"one.sh","no_agent":true,"deliver":"local","prompt":null}]}\r\n'
            with jobs.use_cron_store(home):
                receipt = source_restore.restore_original_store(owner='alice', profile=name,
                    operation_id='restore-' + str(generation), expected_version=version, source_text=source)
            assert path.read_bytes() == source.encode()
            def worker():
                run = subprocess.run([sys.executable, '-c', _ORIGINAL_WORKER],
                    input=json.dumps({'jobId': 'synced', 'instant': None}), text=True,
                    capture_output=True, timeout=30, cwd=checkout, env=env)
                assert run.returncode == 0, run.stderr
                value = json.loads(run.stdout.strip().split('\n')[-1])
                assert not value['processed'] and value['execution']['delivery_outcome'] == 'suppressed'
                assert not effect.exists()
            # Exact enabled/state source may restore, but a fresh worker has no local-effect fallback.
            worker()
            anchor = {'profile': name, 'sourceRevision': 1, 'sourceDigest': hashlib.sha256(source.encode()).hexdigest(),
                      'authorityRevision': 1, 'nativeVersion': hashlib.sha256(path.read_bytes()).hexdigest()}
            bound = cli({'owner': 'alice', 'binding': anchor})
            assert bound['ok'], bound
            # Passive replicas retain source custody; the actual cloud claim refuses effects.
            worker()
            assert calls[-1][2]['action'] == 'claim'
            assert all(c[2]['action'] not in ('select', 'transition') for c in calls[first:])
            # An acknowledged restore does not overwrite the original writer's newer counters.
            newer = path.read_bytes()
            with jobs.use_cron_store(home):
                assert source_restore.restore_original_store(owner='alice', profile=name,
                    operation_id='restore-' + str(generation), expected_version=version, source_text=source) == receipt
            assert path.read_bytes() == newer

        # The same restored original record runs only after this credential is selected.
        mode['value'] = 'ok'
        run = subprocess.run([sys.executable, '-c', _ORIGINAL_WORKER],
            input=json.dumps({'jobId': 'synced', 'instant': None}), text=True,
            capture_output=True, timeout=30, cwd=checkout, env=env)
        assert run.returncode == 0, run.stderr
        value = json.loads(run.stdout.strip().split('\n')[-1])
        assert value['processed'] and value['execution']['status'] == 'completed'
        assert effect.read_text() == 'x'
        assert [c[2]['action'] for c in calls[-3:]] == ['claim', 'transition', 'transition']
