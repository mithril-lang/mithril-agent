"""Retain original source custody privately; clocks/counters use original Cron semantics."""
from copy import deepcopy
import hashlib
import json
import re

from cron import jobs
from cron.source_restore import _read, _parse, _rows
from cron.execution_bindings import install_execution_bindings, read_execution_binding_snapshot
from hermes_constants import get_hermes_home, profile_name_for_home
from .client import integer, validate_command
from .execution import _ATTEMPT_FIELDS

_BOOKKEEPING = _ATTEMPT_FIELDS | {
    'next_run_at', 'last_run_at', 'last_status', 'last_error', 'last_delivery_error',
    'last_delivery_unverified', 'failure_streak', 'last_fire_error', 'last_dispatch',
    'latest_execution', 'preflight_alerted',
}
SOURCE_FIELDS = {'kind', 'policy', 'owner', 'profile', 'sourceRevision', 'sourceDigest',
                 'authorityRevision', 'definitionDigest'}


def definition_digest(job):
    # Use the original read normalization, not a second schedule parser/projection.
    normalized = jobs._normalize_job_record(deepcopy(job))
    value = {key: v for key, v in normalized.items() if key not in _BOOKKEEPING}
    if isinstance(value.get('repeat'), dict):
        value['repeat'] = {key: v for key, v in value['repeat'].items() if key != 'completed'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def bind_original_source(owner, anchor, call):
    """Native-only producer after portable publication and local resource verification."""
    if (not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', owner)
            or not isinstance(anchor, dict)
            or set(anchor) != {'profile', 'sourceRevision', 'sourceDigest', 'authorityRevision', 'nativeVersion'}
            or not isinstance(anchor['profile'], str)
            or profile_name_for_home(get_hermes_home()) != anchor['profile']
            or not integer(anchor['sourceRevision'], 1) or not integer(anchor['authorityRevision'], 1)
            or not all(isinstance(anchor[key], str) and re.fullmatch(r'[a-f0-9]{64}', anchor[key])
                       for key in ('sourceDigest', 'nativeVersion'))):
        raise ValueError('schedule_binding_unconfirmed')
    status_command = {'action': 'status', 'profile': anchor['profile']}
    validate_command(status_command)
    selected = call(status_command)
    if (not selected['ok'] or not selected['receipt']['selected']
            or selected['receipt']['revision'] != anchor['authorityRevision']):
        raise ValueError('schedule_authority_unconfirmed')
    source = _read(jobs._current_cron_store().jobs_file)
    if source is None or hashlib.sha256(source).hexdigest() != anchor['nativeVersion']:
        raise ValueError('schedule_source_changed')
    previous, previous_version = read_execution_binding_snapshot(allow_uninitialized=True)
    for old in (previous or {}).values():
        if (set(old) != SOURCE_FIELDS or old['owner'] != owner or old['profile'] != anchor['profile']
                or old['policy'] != 'mithril-schedules' or old['kind'] != 'original-source-v1'
                or old['sourceRevision'] > anchor['sourceRevision']
                or old['authorityRevision'] > anchor['authorityRevision']
                or (old['sourceRevision'] == anchor['sourceRevision'] and old['sourceDigest'] != anchor['sourceDigest'])):
            raise ValueError('schedule_binding_conflict')
    value = {row['id']: {'kind': 'original-source-v1', 'policy': 'mithril-schedules',
             'owner': owner, 'profile': anchor['profile'], 'sourceRevision': anchor['sourceRevision'],
             'sourceDigest': anchor['sourceDigest'], 'authorityRevision': anchor['authorityRevision'],
             'definitionDigest': definition_digest(row)} for row in _rows(_parse(source))}
    digest = install_execution_bindings(value, anchor['nativeVersion'], previous_version)
    return {'owner': owner, **anchor, 'bindingDigest': digest}


def resolve_source_binding(job, binding):
    """Resolve a retained source against this original execution, never current wall-clock time."""
    from cron.executions import get_execution
    from cron.occurrences import scheduled_instant
    from .execution import bound_job_digest
    if (set(binding) != SOURCE_FIELDS or binding['kind'] != 'original-source-v1'
            or binding['policy'] != 'mithril-schedules'
            or profile_name_for_home(get_hermes_home()) != binding['profile']
            or binding['definitionDigest'] != definition_digest(job)):
        raise RuntimeError('schedule_binding_unconfirmed')
    row = get_execution(str(job.get('execution_id', '')))
    if not row or row['job_id'] != job.get('id'):
        raise RuntimeError('schedule_binding_unconfirmed')
    instant = scheduled_instant(job.get('_scheduled_instant'))
    if instant is None:
        # Original manual executions carry no scheduled slot. Their durable
        # claimed_at names this explicit attempt; never substitute wall time.
        if job.get('_scheduled_instant') is not None or row['scheduled_instant'] is not None:
            raise RuntimeError('schedule_binding_unconfirmed')
        instant = scheduled_instant(row['claimed_at'])
    elif instant != row['scheduled_instant']:
        raise RuntimeError('schedule_binding_unconfirmed')
    return {'policy': 'mithril-schedules', 'owner': binding['owner'], 'jobDigest': bound_job_digest(job),
            'occurrence': {'profile': binding['profile'], 'jobId': job['id'],
                           'operationId': job['execution_id'], 'scheduledInstant': instant,
                           'sourceRevision': binding['sourceRevision'], 'sourceDigest': binding['sourceDigest'],
                           'authorityRevision': binding['authorityRevision']}}
