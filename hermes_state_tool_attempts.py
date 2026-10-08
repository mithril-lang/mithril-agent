"""Private session-owned metadata for actual host tool dispatch attempts."""

import time


class SessionToolAttemptsMixin:
    def begin_tool_attempt(self, session_id, attempt_id, parent_call_id, tool_name, request_digest):
        def claim(conn):
            cursor = conn.execute("""INSERT OR IGNORE INTO session_tool_attempts
                (session_id, attempt_id, parent_call_id, tool_name, request_digest, state, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (session_id, attempt_id, parent_call_id, tool_name, request_digest, time.time()))
            return cursor.rowcount == 1
        return self._execute_write(claim)

    def dispatch_tool_attempt(self, session_id, attempt_id):
        return self._write_rowcount("""UPDATE session_tool_attempts SET state='running', dispatched_at=?
            WHERE session_id=? AND attempt_id=? AND state='pending'""",
            (time.time(), session_id, attempt_id)) == 1

    def settle_tool_attempt(self, session_id, attempt_id, state, result_digest=None, result_bytes=None):
        if state not in {"blocked", "rejected", "not-dispatched", "returned", "returned-error"}:
            raise ValueError("Invalid tool attempt settlement")
        expected = "pending" if state in {"blocked", "rejected", "not-dispatched"} else "running"
        return self._write_rowcount("""UPDATE session_tool_attempts
            SET state=?, settled_at=?, result_digest=?, result_bytes=?
            WHERE session_id=? AND attempt_id=? AND state=?""",
            (state, time.time(), result_digest, result_bytes, session_id, attempt_id, expected)) == 1

    def get_tool_attempt(self, session_id, attempt_id):
        row = self._read_one("""SELECT * FROM session_tool_attempts
            WHERE session_id=? AND attempt_id=?""", (session_id, attempt_id))
        if row is None:
            return None
        result = dict(row)
        # An interrupted host may have executed the effect; pending/running records
        # never authorize replay or imply successful delivery/confirmed cancellation.
        result["terminal"] = result["state"] in {"blocked", "rejected", "not-dispatched", "returned", "returned-error"}
        return result
