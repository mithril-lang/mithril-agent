"""Real HTTP/profile discovery tests for original schedule custody, without execution."""
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
            if mode['value'] == 'lost':
                self.close_connection = True
                return
            if mode['value'] == 'redirect':
                self.send_response(307)
                self.send_header('Location', '/credential-leak')
                self.end_headers()
                return
            value = {'userId': 'alice', 'profile': body['profile']}
            action = body['action']
            if action in ('status', 'select'):
                value.update(selected=True, revision=1)
            else:
                value['operationId'] = body['operationId']
                if action == 'claim':
                    value.update(status='admitted', fresh=True)
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
