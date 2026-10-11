# Desktop profile execution handoff trial

This feature moves a named API trial profile between two registered Desktop
gateways. It preserves the profile name, durable identity, configuration, profile
metadata, SOUL, credentials, SQLite conversation history, session artifacts,
memories, skills and avatar assets. The canonical session titled `Bot Chat` keeps
its durable id and compression lineage. A return move transfers the current
destination history, rather than restoring the old local snapshot.

The initial scope is POSIX gateways and new `handoff-trial-*` profiles. Enroll the
idle profile with `profiles.handoff` before opening its first conversation.
Messaging credentials, a standalone gateway PID and scheduled jobs are refused.
Existing profiles and messaging bots are not enrolled or restarted.

In Desktop, open the trial profile's editor, choose **Execution gateway**, and
select **Move profile**. Both gateways must support `profiles.handoff`. The
management request uses each connection's default gateway, not a profile-pooled
backend. Finish the current turn before moving. Idle conversations are closed
and remain resumable from the transferred store.

## Ownership protocol

1. Resolve the destination's persistent gateway identity.
2. Freeze the source under an exclusive process lease. Agent builds, full turns,
   scoped RPC operations and API listener requests hold shared admission leases.
3. Remove the source profile from the messaging/API multiplexer and cron roster;
   drain its cached agents, shared API memory managers and SQLite handles.
   Failed retirement prevents export.
4. Export a checked SQLite backup and profile files. Encrypt the capsule with an
   ephemeral AES-GCM key; bind the profile name, target gateway, identity,
   generation, operation and content digest. Refuse symlinks, oversized payloads,
   traversal and destination collisions. Stage the destination without execution.
5. Durably release the source. Only then return its activation proof. The
   destination checks that proof and activates the same identity at the next
   generation. Read back both ownership states.

Frozen, moved and staged profiles cannot execute and are excluded from restart
enumeration. A stale activation or a capsule replay under another profile name
cannot revive the old copy. Cached agents reject an earlier ownership generation;
the scoped RPC gateway retires that profile's idle sessions and reopens its store
after a return move, including named pooled backends. Other profiles stay intact.
The Spaces proxy checkpoints the encrypted owner
journal before forwarding transfer acknowledgements, including the release
proof. A checkpoint failure withholds the acknowledgement.

An interrupted move never automatically restarts the source. Retry the same
destination: a frozen source re-exports the same checked snapshot; a released
source returns the same proof for the matching staged operation. No transfer key,
archive or proof is persisted in Desktop. Retained source directories are kept
for recovery and are outside the named profile roster.

## Deployment and validation

Use the local Mac independent `gateway-docker-linux-amd64` pipeline for the exact
source commit. Its regression profile includes handoff, scoped RPC rejection,
restart enumeration, encrypted checkpoint barriers and shared API memory-manager
retirement. Native Desktop tests/builds remain separate from Docker admission.

After qualification, deploy only the isolated trial Space. Validate a real
Desktop/local → Spaces → local roundtrip, latest history and settings read-back,
old-owner execution rejection, checkpoint restoration after restart, and an
interrupted transfer retry. Record those live results separately from source
tests; passing source tests does not establish Spaces rollout completion.

The Space must keep one replica/writer and its encrypted persistent checkpoint
store. Independent replica scaling, arbitrary workspace migration, messaging
bot migration, Windows execution handoff and production publication are outside
this initial trial.
