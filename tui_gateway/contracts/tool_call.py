"""Explicit owned tool-only invocation; schema readback alone is never admission."""

from typing import Literal
from pydantic import Field

from .base import JsonValue, Params, Result
from .registry import method


class ToolsCallParams(Params):
    session_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, JsonValue]
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    context_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    timeout_ms: int = Field(default=120000, ge=1, le=120000, strict=True)


class ToolsCallResult(Result):
    protocol: Literal["hermes-owned-tool-call-v1"]
    attempt_id: str
    state: Literal["pending", "running", "blocked", "rejected", "not-dispatched", "returned", "returned-error"]
    terminal: bool
    duplicate: bool
    observation: Literal["handler-return", "policy-result", "metadata-only", "unknown"]
    output: JsonValue | None


method("tools.call", params=ToolsCallParams, result=ToolsCallResult,
       doc="Invoke one tool through the attached agent policy without model inference; replay returns metadata only.")
