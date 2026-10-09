#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands."""

from __future__ import annotations

import json
import hashlib
import hmac
import re
from typing import List, Optional

from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
        if subsystem == wa.MEMORY:
            lines.extend(f"      {line}" for line in _memory_review_lines(r["payload"]))
    lines.append("")
    lines.append(f"Apply: /{subsystem} approve <id>   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


def _memory_review_lines(payload: dict) -> List[str]:
    """Review the full staged operation, not the 120-character queue summary.

    Keep each new entry JSON-quoted so newlines/control characters cannot look
    like another proposal or command. Destructive operations retain their
    existing full pinned-entry disclosure and legacy-target warning.
    """
    target = payload.get("target", "memory")
    label = {"memory": "MEMORY.md", "user": "USER.md"}.get(target, "unknown target")
    lines = [f"Target: {label}"]
    operations = payload.get("operations", []) if payload.get("action") == "batch" else [payload]
    for index, op in enumerate(operations, 1):
        action = op.get("action", "unknown")
        lines.append(f"Operation {index}: {action}")
        if action in {"add", "replace"}:
            content = op.get("content") or op.get("new_text") or ""
            lines.append(f"Complete new entry: {json.dumps(content, ensure_ascii=False)}")
        lines.extend(_matched_entries(op))
    return lines


def handle_pending_subcommand(
    subsystem: str, args: List[str], *, memory_store=None, set_mode_fn=None) -> Optional[str]:
    """Dispatch a /memory or /skills write-approval subcommand.

    ``memory_store`` applies approved memory writes (CLI passes its live store; gateway a freshly
    loaded one); ``set_mode_fn`` persists the write_approval boolean. Returns text for the user,
    or None when the args are not a write-approval subcommand so the caller falls through to its
    other handling (e.g. /skills search).
    """
    if not args:
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)
    sub, rest = args[0].lower(), args[1:]
    if sub == "pending":
        return _fmt_pending_list(subsystem)
    if sub == "review" and subsystem == wa.MEMORY:
        return _review_memory(rest)
    if subsystem == wa.MEMORY and sub in {"resolve-saved", "resolve-unsaved"}:
        from hermes_cli.write_approval_recovery import resolve
        return resolve(rest, sub.removeprefix("resolve-"))
    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)
    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)
    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)
    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)
    return None  # not ours — caller handles


def _usage(subsystem: str) -> str:
    return f"Usage: /{subsystem} approve|reject <id>  (or 'all')"


def _memory_review_state(record: dict):
    from tools.memory_tool import load_on_disk_store
    return load_on_disk_store().review_state(record.get("payload", {}).get("target", "memory"))


def _review_digest(subsystem: str, record: dict, store_state=None) -> str:
    """Bind reviewed record bytes to the selected profile; never disclose its path."""
    from hermes_constants import get_hermes_home
    data = {"profile": str(get_hermes_home().resolve()), "subsystem": subsystem, "record": record}
    if subsystem == wa.MEMORY:
        data["storeState"] = _memory_review_state(record) if store_state is None else store_state
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _review_memory(rest: List[str]) -> str:
    if not rest:
        records = [r for r in wa.list_pending(wa.MEMORY)
                   if isinstance(r.get("id"), str) and re.fullmatch(r"[a-f0-9]{8}", r["id"])]
        return json.dumps({"protocol": "hermes-pending-memory-review-v1", "pending": [{"pending_id": r["id"], "summary": str(r.get("summary", ""))[:120]}
                                        for r in records[:100]],
                           "remaining_count": max(0, len(records) - 100)}, ensure_ascii=False)
    if len(rest) != 1 or not re.fullmatch(r"[a-f0-9]{8}", rest[0]):
        return "Usage: /memory review <id>"
    from tools.write_approval_decisions import pending_decision_lock, decision_receipt
    try:
        with pending_decision_lock(wa.MEMORY, rest[0]):
            if decision_receipt(wa.MEMORY, rest[0]) is not None:
                from hermes_cli.write_approval_recovery import review
                return review(rest[0])
            record = wa.get_pending(wa.MEMORY, rest[0])
            if not record:
                return f"No pending memory write with id '{rest[0]}'."
            return json.dumps({"protocol": "hermes-pending-memory-review-v1", "pending_id": rest[0], "review_digest": _review_digest(wa.MEMORY, record),
                               "review": _memory_review_lines(record["payload"])}, ensure_ascii=False)
    except Exception:
        return "Pending memory review could not be confirmed; nothing was applied."


def _review_error(subsystem: str, rest: List[str], record: dict, store_state=None) -> Optional[str]:
    """Optional digest leaves existing one-argument CLI approval compatible.

    A remote human-review client must require the reviewed digest, never infer
    consent from it. Decision callers hold the queue lock across this comparison,
    claim, application and retirement. Store-route/entry checks still apply;
    the checked store revision is also compared under its mutation lock. External
    filesystem rollback and external-provider transactions remain separate.
    """
    if subsystem != wa.MEMORY or len(rest) == 1:
        return None
    if len(rest) != 2 or not re.fullmatch(r"[a-f0-9]{64}", rest[1]):
        return "Invalid memory review digest; review the proposal again."
    try:
        if hmac.compare_digest(rest[1], _review_digest(subsystem, record, store_state)):
            return None
    except Exception:
        return "Pending memory review could not be confirmed; nothing was applied."
    return "Pending memory proposal or store changed since review; review it again. Nothing was applied."


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    if subsystem == wa.MEMORY and len(rest) != 1 and not re.fullmatch(r"[a-f0-9]{8}", target):
        return "A reviewed memory decision must select one proposal."
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed, overwritten, removed = 0, [], [], []
    for rec in targets:
        decision_args = rest if target.lower() != "all" else [rec["id"]]
        ok, msg, result = _decide_one(subsystem, rec["id"], decision_args, "approve", memory_store)
        if ok:
            applied += 1
            overwritten.extend(f"  {rec['id']}: {text}" for text in _changed_entries(result, "replaced"))
            removed.extend(f"  {rec['id']}: {text}" for text in _changed_entries(result, "removed"))
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if overwritten:
        # A memory 'replace' overwrites the WHOLE matched entry (#117952); the approver
        # is the last person who can notice a clause went missing, so show what was lost.
        out.append("Overwrote entire entry (re-add anything you still need):")
        out.extend(overwritten)
    if removed:
        out.append("Removed entry (re-add anything you still need):")
        out.extend(removed)
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _changed_entries(result: dict, kind: str) -> List[str]:
    """Full text of every entry a memory replace overwrote (``kind="replaced"``) or remove
    deleted (``"removed"``), single-op or batch shape."""
    single = result.get(f"{kind}_entry")
    batch = result.get(f"{kind}_entries") or {}
    return ([single] if single else []) + [batch[k] for k in sorted(batch, key=int)]


def _matched_entries(payload) -> List[str]:
    """The full entry each staged memory replace/remove is pinned to: the summary shows only
    the old_text search string, and approval applies to this entry, not to that search."""
    from tools.memory_tool import destructive_ops
    return [f"{op['action']}s entry: {op['matched_entry']}" if op.get("matched_entry")
            else f"{op['action']}: unpinned legacy target \u2014 reject and recreate before approving"
            for op in destructive_ops(payload)]


def _apply_one(subsystem: str, rec, memory_store):
    """``(ok, error, result)`` — *result* is the applier's full payload (empty on exceptions)."""
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable", {}
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store, memory_route=rec.get("memory_route"))
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
        return bool(result.get("success")), result.get("error", ""), result
    except Exception as e:
        return False, str(e), {}


def _decide_one(subsystem: str, pending_id: str, rest: List[str], decision: str, memory_store=None):
    """Compare and claim the current queue record under cross-process custody."""
    from tools.write_approval_decisions import (
        pending_decision_lock, decision_receipt, write_receipt, clear_failed_claim, finish_decision)
    try:
        with pending_decision_lock(subsystem, pending_id):
            receipt = decision_receipt(subsystem, pending_id)
            if receipt is not None:
                return False, "A previous decision is already recorded or its outcome is unknown; " \
                              "inspect pending and saved data. Nothing was repeated.", {}
            rec = wa.get_pending(subsystem, pending_id)
            if not rec:
                return False, f"No pending {subsystem} write with id '{pending_id}'.", {}
            if rec.get("id") != pending_id or rec.get("subsystem") != subsystem:
                return False, "Pending proposal identity changed; nothing was applied.", {}
            saved_state = _memory_review_state(rec) if subsystem == wa.MEMORY and len(rest) != 1 else None
            if error := _review_error(subsystem, rest, rec, saved_state):
                return False, error, {}
            if decision == "approve" and subsystem == wa.MEMORY and memory_store is None:
                return False, "memory store unavailable", {}
            write_receipt(subsystem, rec, decision, "applying" if decision == "approve" else "rejecting")
            if decision == "reject":
                finish_decision(subsystem, rec, decision)
                return True, "", {}
            from tools.memory_tool_revision import reviewed_state
            with reviewed_state(saved_state):
                ok, message, result = _apply_one(subsystem, rec, memory_store)
            if not ok:
                if subsystem == wa.MEMORY and result:
                    # Built-in memory validation failures explicitly report no write.
                    clear_failed_claim(subsystem, pending_id)
                    return False, message, result
                write_receipt(subsystem, rec, decision, "unknown")
                return False, "Pending decision outcome is unknown; inspect saved data. Nothing will be repeated.", {}
            finish_decision(subsystem, rec, decision)
            return True, "", result
    except (OSError, ValueError):
        return False, "Pending decision persistence could not be confirmed; inspect pending and saved data. " \
                      "Nothing will be automatically repeated.", {}


def _reject(subsystem: str, rest: List[str]) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    if subsystem == wa.MEMORY and len(rest) != 1 and not re.fullmatch(r"[a-f0-9]{8}", target):
        return "A reviewed memory decision must select one proposal."
    if target.lower() == "all":
        results = [_decide_one(subsystem, rec["id"], [rec["id"]], "reject")
                   for rec in wa.list_pending(subsystem)]
        out = f"Rejected {sum(ok for ok, _, _ in results)} pending {subsystem} write(s)."
        failures = [message for ok, message, _ in results if not ok]
        return out + ("\nFailed:\n" + "\n".join(failures) if failures else "")
    ok, message, _ = _decide_one(subsystem, target, rest, "reject")
    return f"Rejected pending {subsystem} write '{target}'." if ok else message


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    return f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n\n" + wa.skill_pending_diff(rec)


_APPROVAL_VALUES = {
    **dict.fromkeys(("on", "true", "yes", "1", "enable", "enabled"), True),
    **dict.fromkeys(("off", "false", "no", "0", "disable", "disabled"), False)}


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem."""
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    enabled = _APPROVAL_VALUES.get(arg)
    if enabled is None:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."
