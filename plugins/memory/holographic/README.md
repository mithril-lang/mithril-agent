# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 9 actions: add, search, probe, related, reason, contradict, update, remove, list |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |

## Owned route isolation

The provider declares its opened SQLite store/connection and retrieval store through the existing memory-provider identity contract. An owned invocation retires when middleware replaces either destination; built-in memory mirrors use the same fence. Returning to the original route does not revive an observed retired approval, and duplicate requests return metadata without redispatch.

The identity read is cheap and performs no database or filesystem I/O. An uninitialized provider declares only its configured destination and empty runtime owners. This protects route ownership, not fact revisions: review-to-commit CAS, multi-process transaction recovery, lifecycle extraction and external-account qualification remain separate requirements. A built-in write that finishes before a mirror route is retired may remain committed; the owned result is unknown and does not replay either write.

## Partial effect descriptions

The provider supplies `get_tool_effect_manifests()` for `fact_store` and `fact_feedback`. Both declare the partial union of memory reads and writes, with unresolved argument descriptors for `/action` and `/fact_id`. These are descriptions, not resolved destinations, grants or data revision checks. The owned snapshot selects this metadata from the actual inline provider; a same-name registry handler cannot supply its effects or target resolver. Missing declarations remain unknown, malformed declarations refuse admission, and an observed declaration change retires existing consent without changing frozen tool schemas.
