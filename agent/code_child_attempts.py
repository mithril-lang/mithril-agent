"""Use the owning agent's existing SessionDB; retain hashes/status, not payloads."""

import hashlib
import json
from pathlib import Path

from hermes_constants import hermes_home_key


def _digest(value):
    encoded = (value if isinstance(value, str) else json.dumps(value,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


class CodeChildAttempts:
    def __init__(self, agent, home, session):
        self.db = getattr(agent, "_session_db", None)
        self.session = session
        if self.db is not None and hermes_home_key(Path(self.db.db_path).parent) != home:
            raise RuntimeError("Child attempt database does not belong to the parent profile")
        if self.db is not None and not session:
            raise RuntimeError("Child attempt database requires the parent session")

    def begin(self, ref, parent, *, target_digest=None):
        if self.db is None:
            return
        intent = {"name": ref.name, "args": ref.args, "task": ref.task_id, "parent": parent.call_id}
        if target_digest is not None:
            intent["target_digest"] = target_digest
        digest, _ = _digest(intent)
        if not self.db.begin_tool_attempt(self.session, ref.call_id, parent.call_id, ref.name, digest):
            raise RuntimeError("Child tool attempt already exists; it must not be redispatched")

    def dispatch(self, ref):
        if self.db is not None and not self.db.dispatch_tool_attempt(self.session, ref.call_id):
            raise RuntimeError("Child tool attempt cannot enter dispatch; it must not be retried")

    def settle(self, ref, state, result):
        if self.db is None:
            return
        digest, size = _digest(result)
        if not self.db.settle_tool_attempt(self.session, ref.call_id, state, digest, size):
            raise RuntimeError("Child tool outcome settlement is unknown; it must not be retried")
