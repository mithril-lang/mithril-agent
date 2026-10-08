"""Owned Desktop prerequisite; admission remains a PM transaction, never config surgery."""
import importlib
import json
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import re
import sys


def _owned_policy():
    from hermes_cli.plugins_discovery import (
        collect_directory_manifests, discover_entrypoint_manifests, resolve_manifest_winners,
    )
    winners = resolve_manifest_winners(collect_directory_manifests() + discover_entrypoint_manifests())
    policy = winners.get("mithril-schedules")
    if (policy is None or policy.source != "bundled"
            or Path(policy.path).resolve() != Path(__file__).resolve().parent):
        raise ValueError("schedule_policy_unconfirmed")


def prepare_policy(owner, request, call):
    """Authorize first, admit only the owned policy, then fence the original source."""
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_cli.plugins_cmd import (
        _get_disabled_set, _get_enabled_set, _plugin_selection_version, _admit_and_save_plugin_sets,
    )
    from .bindings import prepare_original_source
    from .client import receipt
    if (not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", owner)
            or not isinstance(request, dict) or set(request) != {"profile", "nativeVersion"}
            or not isinstance(request["profile"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request["profile"])
            or profile_name_for_home(get_hermes_home()) != request["profile"]
            or (request["nativeVersion"] is not None and
                (not isinstance(request["nativeVersion"], str)
                 or not re.fullmatch(r"[a-f0-9]{64}", request["nativeVersion"])))):
        raise ValueError("schedule_binding_unconfirmed")
    _owned_policy()
    version = _plugin_selection_version()
    enabled, disabled = _get_enabled_set(), _get_disabled_set()
    if "mithril-schedules" in disabled:
        raise ValueError("schedule_policy_disabled")
    command = {"action": "status", "profile": request["profile"]}
    status = call(command)
    if not isinstance(status, dict) or status.get("ok") is not True:
        raise ValueError("schedule_authority_unconfirmed")
    receipt(status.get("receipt"), owner, command)
    _owned_policy()
    admitted = "mithril-schedules" not in enabled
    if admitted:
        _admit_and_save_plugin_sets(enabled | {"mithril-schedules"}, disabled,
                                   expected_config=version, plugin="mithril-schedules")
    _owned_policy()
    # Revalidate authority and source CAS after admission. A failed preparation
    # is never a successful restore, selection or execution receipt.
    result = prepare_original_source(owner, request, call)
    if admitted:
        # The required marker is durable before notifying existing runtimes:
        # failure/unavailability cannot reopen the unguarded original path.
        # Reuse the same activation path as normal plugin enablement, preserving
        # deferred prompt/tool activation for open conversations.
        from hermes_cli.plugins_activation import activate_plugin_now
        activate_plugin_now("mithril-schedules", in_process=False)
    return result


def main():
    try:
        # Direct script invocation works with both Unix and Windows installers.
        # Resolve the existing profile before importing policy/store modules.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        if (len(sys.argv) != 4 or sys.argv[1] != "-p" or sys.argv[3] != "--stdin"
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sys.argv[2])):
            raise ValueError()
        from hermes_cli.profiles import resolve_profile_env
        os.environ["HERMES_HOME"] = resolve_profile_env(sys.argv[2])
        from hermes_cli.cron import _read_cron_source_request
        from agent.secret_scope import get_secret
        module = importlib.import_module("plugins.mithril-schedules.bootstrap")
        client = importlib.import_module("plugins.mithril-schedules.client")
        value = _read_cron_source_request(client.LIMIT)
        if not isinstance(value, dict) or set(value) != {"owner", "prepare"}:
            raise ValueError()
        token = get_secret("MITHRIL_API_KEY", "")
        def call(command):
            return client.call_custody("https://api.mithril.fund", token, value["owner"], command)
        # PM may report progress; the Desktop wire is exactly one bounded JSON
        # receipt and never forwards configuration/resolver diagnostics.
        with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = {"ok": True, "receipt": module.prepare_policy(value["owner"], value["prepare"], call)}
    except Exception:
        result = {"ok": False, "error": "schedule_binding_unconfirmed"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
