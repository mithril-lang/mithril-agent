# Independent Docker gateway qualification

This Mac-controlled profile qualifies the repository Dockerfile for the
`linux/amd64` gateway trial. It does not turn the organization's held full
Python/Rust/JS/native release matrix into a completed production pipeline.
No GitHub Actions execution is used.

The controller runs only a clean committed source. Its fixed recipe streams
an exact Git archive to a disposable runner directory and builds the repository
image without controller credentials, host source mounts or registry login.
The install stamp records the actual source SHA. Test dependency setup uses
Hermes PM and the frozen source lock. The gateway regression suite runs through
`scripts/run_tests.sh` in a separate container with no external network,
2 CPUs, 4 GiB memory, dropped capabilities and no Docker socket.

The controller then drives real image startup as the Hermes user, SQLite WAL
safety and FTS5, API authentication and persistence across restart. The runtime
uses a disposable named volume and no external network. There are no provider
calls. Live inference and platform-specific bot delivery belong to the later
cloud trial.

## Configure the owning Mac

Use a private external owner JSON containing:

```json
{
  "repository": "mithril-lang/mithril-agent",
  "hostname": "ACTUAL_MAC_HOSTNAME",
  "checkout": "/absolute/path/to/clean/checkout",
  "runner": "gad"
}
```

The hostname and canonical checkout must match the actual controlling Mac.
Owner files and the state directory must be owned by the current user and have
mode 0600 and 0700, respectively. Existing SSH known-hosts and the configured
`gad` alias provide transport identity. A trial-specific secondary Docker daemon
may be selected with `socket: "/run/mithril-agent-trial/docker.sock"`; its lifetime,
capacity and memory reservation must be configured separately. The controller
never resets Docker storage or deletes unrelated volumes, containers or images.

```sh
node scripts/standalone/gateway.mjs plan
node --test tests/scripts/standalone/gateway-contract.test.mjs
node scripts/standalone/gateway.mjs verify --state /PRIVATE/gateway-ci --owner /PRIVATE/gateway-owner.json
node scripts/standalone/gateway.mjs admit --state /PRIVATE/gateway-ci --owner /PRIVATE/gateway-owner.json --receipt /PRIVATE/gateway-ci/RUN.json
```

The native amd64 runner must have at least 12 GiB available in its actual Docker
storage. Insufficient capacity fails before archive transfer or build. Each
verification attempt records a private HMAC-signed success or failure receipt
with source, recipe, owner, stages, image identity and completion time. Failure,
changed identities, stale receipts and forged signatures cannot be admitted.
Receipts always have `productionEligible: false`.

Trial publication needs the same qualified image, scoped trial secrets,
resource-owner checks, rollback or an immutable prior artifact, actual rollout
read-back and authenticated live inference. Integrated current main must be
qualified again before production publication. A failed receipt, focused unit
tests or a prototype Docker daemon is not an active release pipeline.
