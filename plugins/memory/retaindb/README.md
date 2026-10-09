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