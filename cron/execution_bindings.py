"""Private, profile-scoped execution policies without changing original job bytes.

A required marker survives a missing/damaged policy store. Once a profile is
bound, a missing job binding or unloaded policy never restores unguarded effects.
Plugin-specific custody/definition validation stays in the registered policy.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
import stat

from cron import jobs
from cron.source_restore import _checked, _read, _parse, _rows, _LIMIT
from utils import atomic_write_text

_MARKER = '{"schemaVersion":1}'


def _paths():
    directory = jobs._current_cron_store().cron_dir
    return directory / 'execution-policy-required.json', directory / 'execution-bindings.json'


def _private_read(path):
    data = _read(path)
    if data is not None:
        info = path.lstat()
        if (os.name != 'nt' and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid())):
            raise ValueError('execution_policy_storage_unconfirmed')
    return data


def _bindings(value):
    if not isinstance(value, dict) or len(value) > 10000:
        raise ValueError('execution_policy_binding_unconfirmed')
    for job_id, binding in value.items():
        if (not isinstance(job_id, str) or not 0 < len(job_id) <= 256
                or not isinstance(binding, dict) or not isinstance(binding.get('policy'), str)
                or not binding['policy'].strip()):
            raise ValueError('execution_policy_binding_unconfirmed')
    # JSON roundtrip also forbids non-finite plugin metadata and non-JSON values.
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > _LIMIT:
        raise ValueError('execution_policy_binding_unconfirmed')
    return deepcopy(value)


def read_execution_binding_snapshot(*, allow_uninitialized=False):
    """Read current profile policies. Only the locked installer may repair a missing store."""
    marker, path = _paths()
    required = _private_read(marker)
    raw = _private_read(path)
    if required is None and raw is None:
        return None, None
    if required is None or required.decode() != _MARKER:
        raise ValueError('execution_policy_storage_unconfirmed')
    if raw is None:
        if allow_uninitialized:
            return None, None
        raise ValueError('execution_policy_storage_unconfirmed')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('execution_policy_storage_unconfirmed')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {'schemaVersion', 'bindings'} or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise ValueError('execution_policy_storage_unconfirmed')
    return _bindings(value['bindings']), hashlib.sha256(raw).hexdigest()


def read_execution_bindings(*, allow_uninitialized=False):
    return read_execution_binding_snapshot(allow_uninitialized=allow_uninitialized)[0]


def resolve_execution_binding(job):
    """Attach durable metadata to this attempt only, preserving the original source file."""
    bindings = read_execution_bindings()
    if bindings is None:
        return job
    binding = bindings.get(job.get('id'))
    if binding is None:
        raise ValueError('execution_policy_binding_unconfirmed')
    result = deepcopy(job)
    result['_execution_binding'] = deepcopy(binding)
    return result


def _install(bindings, expected_source_version, expected_binding_version, *, require_inventory):
    """Bind an exact idle original inventory under fire fences then its strict jobs lock.

    This grants no permission: the required registered policy must still admit
    each occurrence. Installation cannot change an authored job or its schedule.
    """
    value = _bindings(bindings)
    text = json.dumps({'schemaVersion': 1, 'bindings': value}, ensure_ascii=False, allow_nan=False)
    if len(text.encode()) > _LIMIT:
        raise ValueError('execution_policy_binding_unconfirmed')
    store = jobs._current_cron_store()
    before = _read(store.jobs_file)
    actual_version = hashlib.sha256(before).hexdigest() if before is not None else None
    if actual_version != expected_source_version or (require_inventory and before is None):
        raise ValueError('execution_policy_source_changed')
    rows = _rows(_parse(before)) if before is not None else []
    if require_inventory and set(value) != {row['id'] for row in rows}:
        raise ValueError('execution_policy_inventory_changed')
    marker, path = _paths()
    with ExitStack() as fences:
        for job_id in sorted(row['id'] for row in rows):
            if not fences.enter_context(jobs._fire_job_lock(job_id, wait=False)):
                raise ValueError('execution_policy_busy')
        with jobs._jobs_lock(require_cross_process=True):
            _checked(jobs._jobs_lock_file())
            if _read(store.jobs_file) != before:
                raise ValueError('execution_policy_source_changed')
            now = jobs._hermes_now()
            for row in rows:
                if (jobs._job_running_in_this_process(row['id']) or row.get('pending_slot') is not None
                        or jobs._claim_is_live(row.get('run_claim'), now, jobs._oneshot_run_claim_ttl_seconds())
                        or jobs._claim_is_live(row.get('fire_claim'), now, jobs.FIRE_CLAIM_TTL_SECONDS)):
                    raise ValueError('execution_policy_busy')
            # Preflight both private destinations before the first durable change.
            _private_read(marker)
            current = _private_read(path)
            if (hashlib.sha256(current).hexdigest() if current is not None else None) != expected_binding_version:
                raise ValueError('execution_policy_binding_changed')
            _checked(marker)
            _checked(path)
            atomic_write_text(marker, _MARKER, mode=0o600, preserve_mode=False, fsync_dir=True)
            atomic_write_text(path, text, mode=0o600, preserve_mode=False, fsync_dir=True)
            return hashlib.sha256(text.encode()).hexdigest()


def install_execution_bindings(bindings, expected_source_version, expected_binding_version):
    """Install complete policies for this exact current original inventory."""
    return _install(bindings, expected_source_version, expected_binding_version, require_inventory=True)


def require_execution_policy(expected_source_version, expected_binding_version):
    """Fence this original profile before restoration or executor selection.

    Retain all previous policies. An empty policy map is a required, inactive
    lane, so new original records can retain enabled/state bytes without effects
    until a registered policy admits their exact definitions and occurrences.
    """
    previous, version = read_execution_binding_snapshot(allow_uninitialized=True)
    if version != expected_binding_version:
        raise ValueError('execution_policy_binding_changed')
    return _install(previous or {}, expected_source_version, expected_binding_version, require_inventory=False)
