"""Full human saved-data review closes interrupted decisions without replay."""
import json

import pytest


@pytest.mark.parametrize('target', ['memory', 'user'])
@pytest.mark.parametrize('landed', [False, True])
@pytest.mark.parametrize('choice', ['saved', 'unsaved'])
def test_human_closure_never_runs_another_write(tmp_path, target, landed, choice):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from tools.write_approval_decisions import pending_decision_lock, write_receipt, decision_receipt
    from hermes_cli.write_approval_commands import handle_pending_subcommand as command
    from tui_gateway.server import _session_profile_runtime_scope
    for visit, owner in enumerate(('a', 'b', 'a')):
        home = tmp_path / owner; home.mkdir(exist_ok=True)
        with _session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            content = f'uncertain-{owner}-{visit}: 全文'
            record = wa.stage_write(wa.MEMORY, {'action': 'add', 'target': target, 'content': content}, summary=content, origin='foreground')
            with pending_decision_lock(wa.MEMORY, record['id']):
                write_receipt(wa.MEMORY, record, 'approve', 'applying')
                if landed: assert MemoryStore().add(target, content)['success']
            store = load_on_disk_store(); before = store.review_state(target); entries = list(store._entries_for(target))
            frozen = store._system_prompt_snapshot.copy()
            review = json.loads(command(wa.MEMORY, ['review', record['id']]))
            assert review['decision_mode'] == 'resolve'
            assert content in '\n'.join(review['review'])
            assert json.dumps(decision_receipt(wa.MEMORY, record['id']), ensure_ascii=False, sort_keys=True) in '\n'.join(review['review'])
            assert json.dumps(entries, ensure_ascii=False) in '\n'.join(review['review'])
            # Closure is the human assessment, not an inferred effect or another approval.
            output = command(wa.MEMORY, ['resolve-' + choice, record['id'], review['review_digest']])
            assert 'No memory write was run' in output
            assert wa.get_pending(wa.MEMORY, record['id']) is None
            assert decision_receipt(wa.MEMORY, record['id'])['resolution'] == choice
            assert load_on_disk_store().review_state(target) == before
            assert load_on_disk_store()._entries_for(target) == entries
            assert store._system_prompt_snapshot == frozen
            assert 'No memory write was run' not in command(wa.MEMORY, ['resolve-' + choice, record['id'], review['review_digest']])


@pytest.mark.parametrize('change', ['data', 'proposal', 'receipt', 'foreign', 'malformed'])
def test_closure_requires_the_same_full_outcome_review(tmp_path, change):
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore, load_on_disk_store
    from tools.write_approval_decisions import pending_decision_lock, write_receipt, receipt_path
    from hermes_cli.write_approval_commands import handle_pending_subcommand as command
    from tui_gateway.server import _session_profile_runtime_scope
    a = tmp_path / 'a'; a.mkdir(); b = tmp_path / 'b'; b.mkdir()
    with _session_profile_runtime_scope({'profile_home': str(a)}, hydrate_secrets=False):
        record = wa.stage_write(wa.MEMORY, {'action': 'add', 'content': 'uncertain fact'}, summary='fact', origin='foreground')
        with pending_decision_lock(wa.MEMORY, record['id']): write_receipt(wa.MEMORY, record, 'approve', 'unknown')
        review = json.loads(command(wa.MEMORY, ['review', record['id']]))
        if change == 'data': assert MemoryStore().add('memory', 'new fact')['success']
        if change == 'proposal': wa.atomic_json_write(wa._pending_path(wa.MEMORY, record['id']), {**record, 'summary': 'changed'})
        if change == 'receipt':
            receipt = json.loads(receipt_path(wa.MEMORY, record['id']).read_text()); receipt['updatedAt'] += 1
            wa.atomic_json_write(receipt_path(wa.MEMORY, record['id']), receipt)
    selected = b if change == 'foreign' else a
    with _session_profile_runtime_scope({'profile_home': str(selected)}, hydrate_secrets=False):
        if change == 'foreign':
            wa.atomic_json_write(wa._pending_path(wa.MEMORY, record['id']), record)
            with pending_decision_lock(wa.MEMORY, record['id']): write_receipt(wa.MEMORY, record, 'approve', 'unknown')
        digest = 'invalid' if change == 'malformed' else review['review_digest']
        assert 'No memory write was run' not in command(wa.MEMORY, ['resolve-saved', record['id'], digest])
        assert wa.get_pending(wa.MEMORY, record['id']) is not None
        assert load_on_disk_store().memory_entries == (['new fact'] if change == 'data' else [])
