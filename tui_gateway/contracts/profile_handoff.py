"""Opt-in API trial profile ownership transfer."""
from typing import Literal
from .base import Params, Result
from .registry import method


class HandoffCapsule(Result):
    profile_name: str
    identity: str
    generation: int
    operation: str
    target: str
    sha256: str
    release_hash: str
    archive: str


class HandoffParams(Params):
    action: Literal['gateway', 'status', 'enroll', 'freeze', 'export', 'stage', 'release', 'activate']
    name: str | None = None
    profile: str | None = None
    operation: str | None = None
    target: str | None = None
    sha256: str | None = None
    proof: str | None = None
    encryption_key: str | None = None
    capsule: HandoffCapsule | None = None


class HandoffResult(Result):
    phase: Literal['unmanaged', 'active', 'frozen', 'moved', 'staged'] | None = None
    identity: str | None = None
    generation: int | None = None
    operation: str | None = None
    sha256: str | None = None
    target: str | None = None
    gateway: str | None = None
    proof: str | None = None
    capsule: HandoffCapsule | None = None


method('profiles.handoff', params=HandoffParams, result=HandoffResult,
       doc='Move an idle enrolled named API trial profile with a durable execution fence.')
