"""Concrete policy for a privately bound original scheduler occurrence."""
import hashlib
import json
from copy import deepcopy
from agent.secret_scope import get_secret
from .client import OCCURRENCE, call_custody, epoch_fields, validate_command

# These are attempt-local fields, never authored schedule/resource definitions.
_ATTEMPT_FIELDS = {'_execution_binding', 'execution_id', '_scheduled_instant',
                   'run_claim', 'fire_claim', 'pending_slot'}


def bound_job_digest(job):
    value = {k: v for k, v in job.items() if k not in _ATTEMPT_FIELDS}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def execute_bound_occurrence(ctx, *, args, next_call, **_):
    job = args
    binding = deepcopy(job.get('_execution_binding'))
    source_binding = isinstance(binding, dict) and binding.get('kind') == 'original-source-v1'
    if source_binding:
        from .bindings import resolve_source_binding
        binding = resolve_source_binding(job, binding)
    if (not isinstance(binding, dict) or set(binding) != {'policy', 'owner', 'jobDigest', 'occurrence'}
            or binding['policy'] != 'mithril-schedules'
            or binding['jobDigest'] != bound_job_digest(job)):
        raise RuntimeError('schedule_binding_unconfirmed')
    occurrence = binding['occurrence']
    if not isinstance(occurrence, dict):
        raise RuntimeError('schedule_binding_unconfirmed')
    command = {'action': 'claim', **occurrence} if isinstance(occurrence, dict) else {}
    try:
        if set(occurrence) != epoch_fields(occurrence, OCCURRENCE):
            raise ValueError()
        validate_command(command)
    except (ValueError, TypeError):
        raise RuntimeError('schedule_binding_unconfirmed') from None
    if (occurrence['jobId'] != job.get('id') or occurrence['operationId'] != job.get('execution_id')
            or (not source_binding and occurrence['scheduledInstant'] != job.get('_scheduled_instant'))):
        raise RuntimeError('schedule_binding_unconfirmed')
    token = get_secret('MITHRIL_API_KEY', '')
    api_origin = ctx.get_config('api_origin') or 'https://api.mithril.fund'
    def call(action, **fields):
        return call_custody(api_origin, token, binding['owner'], {'action': action, **occurrence, **fields})
    claimed = call('claim')
    if not claimed['ok'] or not claimed['receipt']['fresh']:
        raise RuntimeError('schedule_occurrence_not_admitted')
    started = call('transition', **{'from': 'admitted', 'to': 'running'})
    if not started['ok'] or not started['receipt']['changed']:
        # A lost start acknowledgement never licenses a second start. Retain the
        # unknown terminal lane if the server did process the first transition.
        call('transition', **{'from': 'running', 'to': 'unknown'})
        raise RuntimeError('schedule_start_unconfirmed')
    try:
        result = next_call(job)
    except BaseException:
        call('transition', **{'from': 'running', 'to': 'unknown'})
        raise
    complete = isinstance(result, dict) and result.get('status') == 'completed'
    finished = call('transition', **{'from': 'running', 'to': 'completed' if complete else 'unknown'})
    if not complete or not finished['ok'] or not finished['receipt']['changed']:
        if complete:
            call('transition', **{'from': 'running', 'to': 'unknown'})
        raise RuntimeError('schedule_completion_unconfirmed')
    return result
