"""Explicit cleanup after terminal assessment persistence never rewrites data/receipt."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest


def stage(target, choice):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore
    from tools.write_approval_decisions import pending_decision_lock, write_receipt
    record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': target, 'content': 'uncertain 全文'}, summary='uncertain', origin='foreground')
    with pending_decision_lock(wa.MEMORY, record['id']):
        write_receipt(wa.MEMORY, record, 'approve', 'applying')
        if choice == 'saved':
            assert MemoryStore().add(target, record['payload']['content'])['success']
    return record


def command(*args):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    return handle_pending_subcommand('memory', list(args))


def reviewed(record):
    return json.loads(command('review', record['id']))


@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('choice', ['saved', 'unsaved'])
@pytest.mark.parametrize('failure', ['receipt-before', 'receipt-after', 'unlink'])
def test_partial_closure_requires_a_fresh_explicit_review(tmp_path, monkeypatch, target, choice, failure):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from tools import write_approval_decisions as decisions
    from tui_gateway.server import _session_profile_runtime_scope
    for owner in ('a', 'b', 'a'):
        home = tmp_path / owner; home.mkdir(exist_ok=True)
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            record = stage(target, choice)
            before = load_on_disk_store().review_state(target)
            original = reviewed(record)
            path = wa._pending_path(wa.MEMORY, record['id'])
            saved_receipt = decisions.atomic_json_write
            saved_unlink = Path.unlink
            def persist(path, *args, **kwargs):
                if failure == 'receipt-before':
                    raise OSError('receipt refused before persistence')
                saved_receipt(path, *args, **kwargs)
                raise OSError('receipt persisted but confirmation lost')
            def unlink(selected, *args, **kwargs):
                if selected == path:
                    raise OSError('queue removal refused')
                return saved_unlink(selected, *args, **kwargs)
            with monkeypatch.context() as fault:
                if failure.startswith('receipt-'):
                    fault.setattr(decisions, 'atomic_json_write', persist)
                else:
                    fault.setattr(Path, 'unlink', unlink)
                assert 'could not be confirmed' in command('resolve-' + choice, record['id'], original['review_digest'])
            assert wa.get_pending(wa.MEMORY, record['id']) == record
            receipt_path = decisions.receipt_path(wa.MEMORY, record['id'])
            prior_receipt_bytes = receipt_path.read_bytes()
            fresh = reviewed(record)
            assert fresh['decision_mode'] == 'resolve'
            if failure != 'receipt-before':
                assert 'Previously recorded assessment: ' + choice in '\n'.join(fresh['review'])
                assert 'Nothing was closed' in command('resolve-' + choice, record['id'], original['review_digest'])
                other = 'unsaved' if choice == 'saved' else 'saved'
                assert 'cannot be changed' in command('resolve-' + other, record['id'], fresh['review_digest'])
                assert receipt_path.read_bytes() == prior_receipt_bytes
            assert 'No memory write was run' in command('resolve-' + choice, record['id'], fresh['review_digest'])
            assert wa.get_pending(wa.MEMORY, record['id']) is None
            assert load_on_disk_store().review_state(target) == before
            if failure != 'receipt-before':
                assert receipt_path.read_bytes() == prior_receipt_bytes
            assert 'No memory write was run' not in command('resolve-' + choice, record['id'], fresh['review_digest'])


@pytest.mark.parametrize('change', ['data', 'proposal', 'receipt', 'id', 'decision', 'state', 'resolution', 'resolution-object'])
def test_terminal_cleanup_refuses_changed_or_inconsistent_review(tmp_path, change):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore
    from tools.write_approval_decisions import pending_decision_lock, receipt_path, write_receipt
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a'; home.mkdir()
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        record = stage('memory', 'saved')
        with pending_decision_lock(wa.MEMORY, record['id']):
            write_receipt(wa.MEMORY, record, 'resolve-saved', 'applied', resolution='saved')
        fresh = reviewed(record)
        path = receipt_path(wa.MEMORY, record['id'])
        if change == 'data':
            assert MemoryStore().add('memory', 'changed after review')['success']
        elif change == 'proposal':
            wa.atomic_json_write(wa._pending_path(wa.MEMORY, record['id']), {**record, 'summary': 'replacement'})
        else:
            receipt = json.loads(path.read_text())
            if change == 'receipt': receipt['updatedAt'] += 1
            if change == 'id': receipt['id'] = 'abcd1234'
            if change == 'decision': receipt['decision'] = 'approve'
            if change == 'state': receipt['state'] = 'unknown'
            if change == 'resolution': receipt['resolution'] = 'automatic'
            if change == 'resolution-object': receipt['resolution'] = {'saved': True}
            wa.atomic_json_write(path, receipt)
        retained = path.read_bytes()
        assert 'No memory write was run' not in command('resolve-saved', record['id'], fresh['review_digest'])
        assert wa.get_pending(wa.MEMORY, record['id']) is not None
        assert path.read_bytes() == retained


def wait_file(path, child):
    deadline = time.monotonic() + 15
    while not path.exists():
        assert child.poll() is None
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('choice', ['saved', 'unsaved'])
def test_real_child_killed_after_assessment_can_be_explicitly_retired(tmp_path, target, choice):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from tools.write_approval_decisions import receipt_path
    from tools.environments.local import served_profile_child_env
    from tui_gateway.server import _session_profile_runtime_scope
    for visit, owner in enumerate(('a', 'b', 'a')):
        home = tmp_path / owner; home.mkdir(exist_ok=True)
        root = tmp_path / f'coord-{visit}'; root.mkdir()
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            record = stage(target, choice)
            review = reviewed(record)
            before = load_on_disk_store().review_state(target)
            config = root / 'worker.json'
            config.write_text(json.dumps({'name': 'closing', 'coordination': str(root), 'pause': 'retire',
                'pending_path': str(wa._pending_path(wa.MEMORY, record['id'])),
                'args': ['resolve-' + choice, record['id'], review['review_digest']]}))
            child = subprocess.Popen([sys.executable, '-m', 'tests.hermes_cli.fixtures.pending_decision_worker', str(config)],
                                     cwd=Path(__file__).resolve().parents[2],
                                     env=served_profile_child_env(target_home=home, inherit_credentials=False))
            try:
                wait_file(root / 'closing.retiring', child)
                persisted = receipt_path(wa.MEMORY, record['id']).read_bytes()
                assert json.loads(persisted)['resolution'] == choice
                assert wa.get_pending(wa.MEMORY, record['id']) == record
                child.kill(); child.wait(timeout=5)
                assert child.returncode != 0
                fresh = reviewed(record)
                assert 'No memory write was run' in command('resolve-' + choice, record['id'], fresh['review_digest'])
                assert wa.get_pending(wa.MEMORY, record['id']) is None
                assert receipt_path(wa.MEMORY, record['id']).read_bytes() == persisted
                assert load_on_disk_store().review_state(target) == before
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
