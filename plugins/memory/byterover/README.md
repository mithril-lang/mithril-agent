# ByteRover Memory Provider

Persistent memory via the `brv` CLI — hierarchical knowledge tree with tiered retrieval (fuzzy text → LLM-driven search).

## Requirements

Install the ByteRover CLI:
```bash
curl -fsSL https://byterover.dev/install.sh | sh
# or
npm install -g byterover-cli
```

## Setup

```bash
hermes memory setup    # select "byterover"
```

Or manually:
```bash
hermes config set memory.provider byterover
# Optional cloud sync:
echo "BRV_API_KEY=your-key" >> ~/.hermes/.env
```

## Config

| Env Var | Required | Description |
|---------|----------|-------------|
| `BRV_API_KEY` | No | Cloud sync key (optional, local-first by default) |

Working directory: `$HERMES_HOME/byterover/` (profile-scoped).

## Tools

| Tool | Description |
|------|-------------|
| `brv_query` | Search the knowledge tree |
| `brv_curate` | Store facts, decisions, patterns |
| `brv_status` | CLI version, tree stats, sync state |

## Owned execution route custody candidate

The provider's existing identity signature includes its initialized working directory and session ID. The owned route fence hashes these privately; changing the working directory after review retires consent before the CLI can run. Reading the signature never resolves availability, creates a directory, launches a process or exposes credentials. Mutable turn counters and stored data do not change route identity or the frozen tool schema.

Local qualification loads the real bundled provider and invokes its actual subprocess path through an explicit local CLI fixture. Query, curate and status across three middleware stages and profile A/B/A refuse stale routes before dispatch, preserve both directories and frozen history/schema, and do not replay. Separately authorized fresh calls execute once under the selected profile's child environment. The previous candidate reports success for all nine stale cases; an independent filesystem witness also confirms a curate effect in the sibling profile's directory. Ten new cases and related suites pass (150 total, retries zero).

The local executable is a qualification fixture, not installed ByteRover or a cloud account. Resolved public targets/effect declarations, CLI binary replacement, mutable data CAS, provider reinitialization/background lifecycle and actual UI/account/native/public/installed/continuous stability remain unqualified.

Owned calls also pin the initialized route for the operation through the existing optional provider binding hook. Background curate pins its route when queued, while the existing context-aware thread carries the selected profile. Four capture-race cases reproduce late foreground and queued redirection on the previous candidate. The local fixture checks actual subprocess destination with independent A/B/A witnesses. Directory pinning is not filesystem/data CAS or asynchronous lease revocation.

The final related qualification passes 153 cases across nine files in 109.1s, retries zero. The capture tests replace the earlier identity-only unit case; previous 150-case evidence remains historical.
