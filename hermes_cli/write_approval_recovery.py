"""Explicit human closure of an uncertain built-in memory decision; never replay."""
import hashlib
import hmac
import json
import re

from hermes_constants import get_hermes_home
from tools import write_approval as wa
from tools.write_approval_decisions import decision_receipt, finish_decision, pending_decision_lock


def _record_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _digest(record, receipt, snapshot):
    data = {'profile': str(get_hermes_home().resolve()), 'record': record, 'receipt': receipt, 'snapshot': snapshot}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _confirmed_record(pending_id):
    record = wa.get_pending(wa.MEMORY, pending_id)
    receipt = decision_receipt(wa.MEMORY, pending_id)
    if not record or not receipt or record.get('id') != pending_id or record.get('subsystem') != wa.MEMORY:
        raise ValueError('No recorded memory decision to resolve.')
    if receipt.get('id') != pending_id or receipt.get('recordDigest') != _record_digest(record):
        raise ValueError('The proposal differs from its recorded decision; no closure was performed.')
    resolution = receipt.get('resolution')
    if resolution is not None and (not isinstance(resolution, str) or resolution not in {'saved', 'unsaved'} or
            receipt.get('decision') != 'resolve-' + resolution or
            receipt.get('state') != ('applied' if resolution == 'saved' else 'rejected')):
        raise ValueError('The recorded assessment is inconsistent; no closure was performed.')
    return record, receipt


def review(pending_id):
    from tools.memory_tool import load_on_disk_store
    from hermes_cli.write_approval_commands import _memory_review_lines
    with pending_decision_lock(wa.MEMORY, pending_id):
        record, receipt = _confirmed_record(pending_id)
        target = record['payload'].get('target', 'memory')
        with load_on_disk_store().locked_review_snapshot(target) as snapshot:
            lines = [*_memory_review_lines(record['payload']),
                     'Recorded decision receipt: ' + json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                     'Current saved entries: ' + json.dumps(snapshot['entries'], ensure_ascii=False),
                     'Confirm the saved result yourself. Closing records your assessment and never reruns the write.']
            if receipt.get('resolution') is not None:
                lines.append('Previously recorded assessment: ' + receipt['resolution'] +
                             '. Confirm the same assessment to remove the remaining queue record; it cannot be changed here.')
            return json.dumps({'protocol': 'hermes-pending-memory-review-v1', 'pending_id': pending_id,
                               'review_digest': _digest(record, receipt, snapshot), 'review': lines,
                               'decision_mode': 'resolve'}, ensure_ascii=False)


def resolve(rest, choice):
    if choice not in {'saved', 'unsaved'} or len(rest) != 2 or not re.fullmatch(r'[a-f0-9]{8}', rest[0]) or not re.fullmatch(r'[a-f0-9]{64}', rest[1]):
        return 'A memory outcome closure requires one reviewed proposal and its digest.'
    from tools.memory_tool import load_on_disk_store
    try:
        with pending_decision_lock(wa.MEMORY, rest[0]):
            record, receipt = _confirmed_record(rest[0])
            with load_on_disk_store().locked_review_snapshot(record['payload'].get('target', 'memory')) as snapshot:
                if not hmac.compare_digest(rest[1], _digest(record, receipt, snapshot)):
                    return 'Memory outcome changed since review; review the saved data again. Nothing was closed.'
                if receipt.get('resolution') is not None and receipt['resolution'] != choice:
                    return 'The recorded assessment cannot be changed here; review and confirm the same assessment. Nothing was closed.'
                finish_decision(wa.MEMORY, record, 'resolve-' + choice, resolution=choice,
                                existing_receipt=receipt if receipt.get('resolution') is not None else None)
                return 'Closed the recorded memory decision as ' + choice + '. No memory write was run.'
    except (OSError, ValueError):
        return 'Memory outcome closure could not be confirmed; inspect the queue and saved data. Nothing will be replayed.'
