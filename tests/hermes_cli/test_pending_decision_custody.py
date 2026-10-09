"""Real process contention and interrupted pending decisions, profile-local A/B/A."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest



def wait_for(path, *, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


def launch(home, coord, name, args, *, pause=None, subsystem="memory"):
    from tools.environments.local import served_profile_child_env
    config = coord / (name + '.config.json')
    config.write_text(json.dumps({'coordination': str(coord), 'name': name, 'args': args, 'pause': pause, 'subsystem': subsystem}))
    env = served_profile_child_env(target_home=home, inherit_credentials=False)
    # Execute as a module so the actual checkout is imported, not the fixture directory.
    child = subprocess.Popen([sys.executable, '-m', 'tests.hermes_cli.fixtures.pending_decision_worker', str(config)],
                             cwd=Path(__file__).resolve().parents[2], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return child


def finish(child):
    out, err = child.communicate(timeout=20)
    assert child.returncode == 0, out + err


def stop(children, coord):
    (coord / 'release').touch()
    for child in children:
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
        assert child.poll() is not None


def proposal(home, target, content):
    from tools import write_approval as wa
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home.mkdir(exist_ok=True)
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': target, 'content': content}, summary=content, origin='foreground')
        review = json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))
    return record, review['review_digest']


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('competing', ['approve', 'reject'])
def test_competing_pending_decisions_apply_once(tmp_path, target, competing):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from tui_gateway.server import _session_profile_runtime_scope
    homes = {owner: tmp_path / owner for owner in ('a', 'b')}
    expected = {'a': [], 'b': []}
    for visit, owner in enumerate(('a', 'b', 'a')):
        content = f'{owner}-{visit}-{target}'
        rec, digest = proposal(homes[owner], target, content)
        coord = tmp_path / f'coord-{visit}'; coord.mkdir()
        children = []
        try:
            first = launch(homes[owner], coord, 'first', ['approve', rec['id'], digest], pause='before'); children.append(first)
            assert wait_for(coord / 'first.entered')
            second = launch(homes[owner], coord, 'second', [competing, rec['id'], digest]); children.append(second)
            assert wait_for(coord / 'second.ready')
            # On the broken path both appliers/reject can finish before release.
            # On the fixed path the second consumer waits for the real queue lock.
            # Correctness is asserted on terminal results, not a negative timing probe.
            wait_for(coord / ('second.entered' if competing == 'approve' else 'second.result'), timeout=2)
            (coord / 'release').touch()
            finish(first); finish(second)
            outputs = [json.loads((coord / (name + '.result')).read_text())['output'] for name in ('first', 'second')]
            assert sum('Approved 1 memory write' in out for out in outputs) == 1, outputs
            assert sum('Rejected pending memory write' in out for out in outputs) == 0, outputs
            assert sum((coord / (name + '.entered')).exists() for name in ('first', 'second')) == 1
            expected[owner].append(content)
            for key, home in homes.items():
                if not home.exists(): continue
                with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
                    assert load_on_disk_store()._entries_for(target) == expected[key]
                    assert wa.list_pending(wa.MEMORY) == []
        finally:
            stop(children, coord)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('pause', ['before', 'after'])
def test_interrupted_decision_is_not_replayed(tmp_path, target, pause):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from tools.write_approval_decisions import decision_receipt
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a'
    record, digest = proposal(home, target, 'interrupted fact')
    coord = tmp_path / 'coord'; coord.mkdir()
    children = []
    try:
        first = launch(home, coord, 'first', ['approve', record['id'], digest], pause=pause); children.append(first)
        assert wait_for(coord / ('first.entered' if pause == 'before' else 'first.committed'))
        first.kill(); first.communicate(timeout=5)
        assert first.poll() is not None
        second = launch(home, coord, 'second', ['approve', record['id'], digest]); children.append(second)
        finish(second)
        output = json.loads((coord / 'second.result').read_text())['output']
        assert 'outcome is unknown' in output and 'Nothing was repeated' in output
        assert not (coord / 'second.entered').exists()
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            assert wa.get_pending(wa.MEMORY, record['id']) == record
            assert decision_receipt(wa.MEMORY, record['id'])['state'] == 'applying'
            assert wa.discard_pending(wa.MEMORY, record['id']) is False
            assert json.loads(handle_pending_subcommand(wa.MEMORY, ['review', record['id']]))['decision_mode'] == 'resolve'
            assert load_on_disk_store()._entries_for(target) == (['interrupted fact'] if pause == 'after' else [])
    finally:
        stop(children, coord)


@pytest.mark.parametrize('landed', [False, True])
def test_unconfirmed_claim_never_enters_applier(tmp_path, monkeypatch, landed):
    from tools import write_approval as wa
    from tools import write_approval_decisions as decisions
    from hermes_cli import write_approval_commands as commands
    from tools.memory_tool import load_on_disk_store
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';record, digest = proposal(home, 'memory', 'claim fact')
    original = decisions.atomic_json_write
    def lose_confirmation(path, data, **kwargs):
        if landed: original(path, data, **kwargs)
        raise OSError('private journal error')
    monkeypatch.setattr(decisions, 'atomic_json_write', lose_confirmation)
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        store = load_on_disk_store()
        output = commands.handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=store)
        assert 'could not be confirmed' in output and 'private journal error' not in output
        assert store._entries_for('memory') == []
        assert wa.get_pending(wa.MEMORY, record['id']) == record
        assert (decisions.decision_receipt(wa.MEMORY, record['id']) is not None) == landed
        assert not store._path_for('memory').exists()


def test_restored_record_cannot_reuse_terminal_decision(tmp_path):
    from tools import write_approval as wa
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import load_on_disk_store
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';record, digest = proposal(home, 'memory', 'reviewed fact')
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        store = load_on_disk_store()
        assert 'Approved 1' in handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=store)
        assert store.remove('memory', 'reviewed fact')['success']
        # A filesystem writer restores identical proposal bytes after its first decision.
        wa.atomic_json_write(wa._pending_path(wa.MEMORY, record['id']), record)
        for decision in ('approve', 'reject'):
            output = handle_pending_subcommand(wa.MEMORY, [decision, record['id'], digest], memory_store=store)
            assert 'Nothing was repeated' in output
        assert load_on_disk_store()._entries_for('memory') == []
        assert wa.get_pending(wa.MEMORY, record['id']) == record


def test_changed_queue_entry_survives_commit_retirement(tmp_path, monkeypatch):
    from tools import write_approval as wa
    from tools.write_approval_decisions import decision_receipt
    from hermes_cli import write_approval_commands as commands
    from tools.memory_tool import load_on_disk_store
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';record, digest = proposal(home, 'memory', 'reviewed fact')
    changed = {**record, 'payload': {**record['payload'], 'content': 'unreviewed replacement'}}
    original = commands._apply_one
    def replace_during_retirement(*args):
        result = original(*args)
        wa.atomic_json_write(wa._pending_path(wa.MEMORY, record['id']), changed)
        return result
    monkeypatch.setattr(commands, '_apply_one', replace_during_retirement)
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        output = commands.handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=load_on_disk_store())
        assert 'could not be confirmed' in output
        assert load_on_disk_store()._entries_for('memory') == ['reviewed fact']
        assert wa.get_pending(wa.MEMORY, record['id']) == changed
        assert decision_receipt(wa.MEMORY, record['id'])['state'] == 'unknown'
        assert 'Nothing was repeated' in commands.handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=load_on_disk_store())


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('competing', ['approve', 'reject'])
def test_skill_decisions_preserve_profile_custody(tmp_path, competing):
    from tools import write_approval as wa
    from tui_gateway.server import _session_profile_runtime_scope
    homes = {owner: tmp_path / owner for owner in ('a', 'b')}
    for visit, owner in enumerate(('a', 'b', 'a')):
        home = homes[owner]; home.mkdir(exist_ok=True)
        name = f'queued-skill-{visit}'
        content = f'---\nname: {name}\ndescription: Local custody qualifier.\n---\nSkill owned by {owner}.\n'
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            record = wa.stage_write(wa.SKILLS, {'action': 'create', 'name': name, 'content': content}, summary=name, origin='foreground')
        coord = tmp_path / f'coord-{visit}'; coord.mkdir(); children = []
        try:
            first = launch(home, coord, 'first', ['approve', record['id']], pause='before', subsystem='skills'); children.append(first)
            assert wait_for(coord / 'first.entered')
            second = launch(home, coord, 'second', [competing, record['id']], subsystem='skills'); children.append(second)
            assert wait_for(coord / 'second.ready')
            wait_for(coord / ('second.entered' if competing == 'approve' else 'second.result'), timeout=2)
            (coord / 'release').touch()
            finish(first); finish(second)
            outputs = [json.loads((coord / (n + '.result')).read_text())['output'] for n in ('first', 'second')]
            assert sum('Approved 1 skills write' in out for out in outputs) == 1, outputs
            assert not any('Rejected pending skills write' in out for out in outputs), outputs
            assert sum((coord / (n + '.entered')).exists() for n in ('first', 'second')) == 1
            assert (home / 'skills' / name / 'SKILL.md').read_text() == content
            foreign = homes['b' if owner == 'a' else 'a'] / 'skills' / name / 'SKILL.md'
            assert not foreign.exists()
            with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
                assert wa.list_pending(wa.SKILLS) == []
        finally:
            stop(children, coord)


def test_unavailable_lock_does_not_apply_or_discard(tmp_path, monkeypatch):
    from tools import write_approval as wa
    from tools import memory_tool
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';record, digest = proposal(home, 'memory', 'locked fact')
    monkeypatch.setattr(memory_tool, 'fcntl', None)
    monkeypatch.setattr(memory_tool, 'msvcrt', None)
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        for decision in ('approve', 'reject'):
            output = handle_pending_subcommand(wa.MEMORY, [decision, record['id'], digest], memory_store=memory_tool.load_on_disk_store())
            assert 'could not be confirmed' in output
        assert wa.get_pending(wa.MEMORY, record['id']) == record
        assert not memory_tool.MemoryStore._path_for('memory').exists()


def test_applier_exception_after_commit_keeps_unknown_claim(tmp_path, monkeypatch):
    from tools import write_approval as wa
    from tools import memory_tool
    from tools.write_approval_decisions import decision_receipt
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope
    home = tmp_path / 'a';record, digest = proposal(home, 'memory', 'committed fact')
    original = memory_tool.apply_memory_pending
    def lose_result(*args, **kwargs):
        assert original(*args, **kwargs)['success']
        raise RuntimeError('private applier error')
    monkeypatch.setattr(memory_tool, 'apply_memory_pending', lose_result)
    with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        output = handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=memory_tool.load_on_disk_store())
        assert 'outcome is unknown' in output and 'private applier error' not in output
        assert decision_receipt(wa.MEMORY, record['id'])['state'] == 'unknown'
        assert memory_tool.load_on_disk_store().memory_entries == ['committed fact']
        assert wa.get_pending(wa.MEMORY, record['id']) == record
        assert 'Nothing was repeated' in handle_pending_subcommand(wa.MEMORY, ['approve', record['id'], digest], memory_store=memory_tool.load_on_disk_store())
