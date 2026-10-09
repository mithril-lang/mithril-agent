"""Real store changes, ABA and pre-apply contention cannot reuse a human review."""
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('change', ['changed', 'restored'])
@pytest.mark.parametrize('action', ['add', 'replace', 'remove', 'batch'])
def test_review_does_not_survive_store_changes(tmp_path, target, change, action):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    for visit, owner in enumerate(('a', 'b', 'a')):
        home = tmp_path / owner; home.mkdir(exist_ok=True)
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            store = MemoryStore(); store.load_from_disk()
            frozen = store._system_prompt_snapshot.copy()
            seed = f'seed-{visit}'
            assert store.add(target, seed)['success']
            baseline = list(store._entries_for(target))
            payload = {'action': action, 'target': target, 'content': f'approved-{visit}'}
            if action in ('replace', 'remove'):
                payload.update(old_text=seed, matched_entry=seed)
            if action == 'batch':
                payload['operations'] = [{'action': 'replace', 'old_text': seed, 'matched_entry': seed, 'content': f'approved-{visit}'}, {'action': 'add', 'content': f'batch-{visit}'}]
            record = wa.stage_write(wa.MEMORY, payload, summary='full proposed entry', origin='foreground')
            review = json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
            fact = f'intervening-{visit}'
            assert store.add(target, fact)['success']
            if change == 'restored': assert store.remove(target, fact)['success']
            output = handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], review['review_digest']], memory_store=load_on_disk_store())
            assert 'changed since review' in output, output
            assert wa.get_pending(wa.MEMORY, record['id']) == record
            expected = baseline + ([fact] if change == 'changed' else [])
            assert load_on_disk_store()._entries_for(target) == expected
            fresh = json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
            assert fresh['review_digest'] != review['review_digest']
            assert 'Approved 1' in handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], fresh['review_digest']], memory_store=load_on_disk_store())
            assert store._system_prompt_snapshot == frozen


@pytest.mark.parametrize('target', ['memory', 'user'])
def test_store_change_after_digest_check_is_refused_under_mutation_lock(tmp_path, monkeypatch, target):
    import subprocess
    import sys
    from tools import write_approval as wa
    from tools.environments.local import served_profile_child_env
    from tools.memory_tool import load_on_disk_store
    from hermes_cli import write_approval_commands as commands
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';home.mkdir()
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': target, 'content': 'reviewed fact'}, summary='fact', origin='foreground')
        review = json.loads(commands.handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
        original = commands._apply_one
        def intervening_writer(*args):
            # A separate process cannot borrow the reviewed-state ContextVar.
            child = subprocess.run([sys.executable, '-c',
                'import json,sys;from tools.memory_tool import MemoryStore;s=MemoryStore();print(json.dumps(s.add(sys.argv[1],"competing fact")))', target],
                cwd=Path(__file__).resolve().parents[2], env=served_profile_child_env(target_home=home, inherit_credentials=False),
                capture_output=True, text=True, timeout=20)
            assert child.returncode == 0, child.stderr
            assert json.loads(child.stdout)['success']
            return original(*args)
        monkeypatch.setattr(commands, '_apply_one', intervening_writer)
        output = commands.handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], review['review_digest']], memory_store=load_on_disk_store())
        assert 'Saved memory changed since review' in output
        assert load_on_disk_store()._entries_for(target) == ['competing fact']
        assert wa.get_pending(wa.MEMORY, record['id']) == record
        monkeypatch.setattr(commands, '_apply_one', original)
        fresh = json.loads(commands.handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
        assert 'Approved 1' in commands.handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], fresh['review_digest']], memory_store=load_on_disk_store())


@pytest.mark.parametrize('target', ['memory', 'user'])
def test_failed_data_write_invalidates_old_review(tmp_path, monkeypatch, target):
    from tools import write_approval as wa
    from tools import memory_tool_store
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a'; home.mkdir()
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': target, 'content': 'reviewed fact'}, summary='fact', origin='foreground')
        review = json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
        original = memory_tool_store.atomic_write_text
        def fail(*args, **kwargs): raise OSError('qualification data write failure')
        monkeypatch.setattr(memory_tool_store, 'atomic_write_text', fail)
        with pytest.raises(RuntimeError): MemoryStore().add(target, 'uncertain fact')
        monkeypatch.setattr(memory_tool_store, 'atomic_write_text', original)
        assert load_on_disk_store()._entries_for(target) == []
        assert 'changed since review' in handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], review['review_digest']], memory_store=load_on_disk_store())


@pytest.mark.parametrize('fault', ['removed', 'corrupt'])
def test_revision_marker_fault_never_revives_review(tmp_path, fault):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a'; home.mkdir()
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': 'memory', 'content': 'reviewed fact'}, summary='fact', origin='foreground')
        review = json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
        marker = home / 'memories/MEMORY.md.revision.json'
        if fault == 'removed': marker.unlink()
        else: marker.write_text('{invalid')
        output = handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], review['review_digest']], memory_store=load_on_disk_store())
        assert 'Approved 1' not in output
        assert load_on_disk_store().memory_entries == []
        assert wa.get_pending(wa.MEMORY, record['id']) == record
