"""Owned attempt metadata readback; no execution or result-body authority."""

from typing import Literal
from pydantic import Field

from .base import Params, Result
from .registry import method


class ToolAttemptsParams(Params):
    session_id: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    before_attempt_id: str | None = Field(default=None, min_length=1, max_length=512)


class ToolAttemptRow(Result):
    attempt_id: str
    parent_call_id: str
    tool_name: str
    state: Literal["pending", "running", "blocked", "rejected", "not-dispatched", "returned", "returned-error"]
    terminal: bool
    created_at: float
    dispatched_at: float | None
    settled_at: float | None
    result_digest: str | None
    result_bytes: int | None


class ToolAttemptsResult(Result):
    protocol: Literal["hermes-tool-attempts-v1"]
    coverage: Literal["exact-session-metadata-only"]
    available: bool
    attempts: list[ToolAttemptRow]
    next_cursor: str | None


method("tools.attempts", params=ToolAttemptsParams, result=ToolAttemptsResult,
       doc="Read a bounded owned session attempt page; handler return is not delivery or confirmed stop.")
