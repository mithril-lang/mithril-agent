# RetainDB Memory Provider

Cloud memory API with hybrid search (Vector + BM25 + Reranking) and 7 memory types.

## Requirements

- RetainDB account ($20/month) from [retaindb.com](https://www.retaindb.com)
- `requests` is part of Hermes's core dependencies; no separate SDK install is needed. For damaged dependencies, use `hermes pm repair` and restart Hermes.

## Setup

```bash
hermes memory setup    # select "retaindb"
```

Or manually:
```bash
hermes config set memory.provider retaindb
echo "RETAINDB_API_KEY=your-key" >> ~/.hermes/.env
```

## Config

All config via environment variables in `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `RETAINDB_API_KEY` | (required) | API key |
| `RETAINDB_BASE_URL` | `https://api.retaindb.com` | API endpoint |
| `RETAINDB_PROJECT` | auto (profile-scoped) | Project identifier |

## Tools

| Tool | Description |
|------|-------------|
| `retaindb_profile` | User's stable profile |
| `retaindb_search` | Semantic search |
| `retaindb_context` | Task-relevant context |
| `retaindb_remember` | Store a fact with type + importance |
| `retaindb_forget` | Delete a memory by ID |

## RetainDB initialized project custody candidate

The selected provider now includes the initialized client instance, endpoint/project and user/session/agent routing in its cheap private identity signature. Credentials and stored memory data are excluded. This uses the existing generic owned identity fence; no tool/schema, core branch, availability probe or resolved target/data CAS is added. In-place project changes before dispatch retire old approval, and observed A/B/A cannot revive it.

The real bundled provider is discovered and initialized under two temporary profile homes. Its actual requests client talks to an explicit loopback HTTP fixture. Nine remember/search/profile cases across all three middleware stages failed on the preceding candidate: the fixture received the sibling project alongside the original profile user. Independent HTTP request records now show stale refusal without sending, metadata-only duplicate, and separately approved fresh execution in the selected project. Frozen schema/history remain unchanged. Canonical related qualification passes 81 cases across five files in 41.5s, file retries zero. The compatibility suite uses fixture responses; no actual vendor account is claimed.

Late mutation after final admission, client credentials/binary replacement, resolved targets/effects, data CAS, all ten operations/file artifacts, write-behind queue custody/reinitialization, cancellation/reconnect, real account/native/UI/public/installed and continuous stability remain unqualified.

## RetainDB operation and live queue route capture

The optional provider binding scope pins a shallow provider/client operation copy to the initialized endpoint/project. Late mutations of the original client cannot redirect the actual handler; generic original-owner validation still suppresses output as unknown when identity changes, and duplicates do not resend. The live write-behind queue snapshots its client at construction, preserving its initialized route when the provider client changes while a row waits. Existing profile-aware threads and SQLite pending row formats are unchanged.

Four capture-race cases first failed with actual loopback HTTP requests to the sibling project: remember/search/profile after admission, and a gated real sync_turn enqueue/flush. The fix and prior project checks pass thirteen A/B/A cases, using independent HTTP records and pending-row readback. Related canonical qualification passes 91 cases in six files/35.9s, file retries zero. Original schema/history remain frozen. Real vendor accounts, all ten operations, direct queue-internal mutation, durable restart/replay route fingerprints, result-loss/fallback behavior, queue lease revocation, remote CAS, native/UI/public/installed and continuing stability remain unqualified.
