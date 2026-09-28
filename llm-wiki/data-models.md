# HSYNC Server — Data & state

The server owns **no** sync data types — those come from `hsync_common.models`
(see [common data-models](../../hsync-common/llm-wiki/data-models.md)). It only adds
process-local state and the installation SQLite store.

## Reused common models

| Type | Used by | Source |
| ---- | ------- | ------ |
| `File` / `FileDict` | list, upload, download responses | `hsync_common.models.file` |
| `Dir` | event handlers (`Dir.from_dict`), list (`Dir.get`) | `hsync_common.models.dir` |
| `Symlink` | symlink event handler | `hsync_common.models.symlink` |
| `Event` / `EventName` | WS broadcast + handlers | `hsync_common.models.event` |
| `TombstoneStore` / `TombstoneEntry` | deletion tracking | `hsync_common.models.tombstone` |

## Server-local state

| State | Location | Lifetime | Persistence |
| ----- | -------- | -------- | ----------- |
| SHA-256 cache `FILE_HASHES` | `hashes.py` | process memory (unbounded dict, not LRU) | rebuilt at startup from disk (`set_sha256_hashes`) |
| Delta signature cache `_signature_cache` | `api/files/delta/signatures.py` | process memory (TTL 300 s, FIFO cap 64, `Lock`-guarded) | none (recomputed on demand) |
| Delta literal-payload tokens `delta_token_store` | `api/files/delta/tokens.py` | process memory (`RLock`-guarded) + payload temp files | none (TTL-reaped) |
| Per-bucket WS pools | `connection_manager.py:187` (`bucket_manager`) | process memory | none (live connections) |
| Tombstone stores (per bucket) | `tombstones.py` cache | process memory + SQLite | per-bucket DB `server/<bucket>.db` (`tombstones` table) |
| Version stores (per bucket) | `versions.py` cache | process memory + SQLite | per-bucket DB `server/<bucket>.db` (`versions` table) |
| Accounts + membership | `auth_store.py` | SQLite | global `server/auth.db` (`clients`, `bucket_members` tables) |

The SHA-256 cache maps `sha256(filepath) -> (file_sha256, mtime_ns, size)`: a
cached digest is served only while the file's current metadata matches the
captured pair, otherwise the entry is dropped and callers recompute (see
[modules/infrastructure.md](modules/infrastructure.md) — Hash cache).

## Accounts and membership (`auth_store.py`)

Server-only tables on the global auth DB `server/auth.db` (not part of the
common `sqlstore` schema):

- **`clients`** — `id` (PK, 21-alnum), `name` (UNIQUE, immutable), `role`
  (`admin`/`client`), `status` (`active`/`disabled`), `key_hash` (PHC scrypt),
  `key_prefix` (8 hex for logs), `key_generation`, `key_expires_at`,
  `prior_key_hash`/`prior_expires_at` (grace rotation), `created_at`,
  `last_used_at`.
- **`bucket_members`** — PK `(bucket, client_id)`, `role` (`owner`/`member`);
  partial UNIQUE index enforces exactly one owner per bucket.

`AuthStore` (`auth_store.py`) is the single source of truth for account CRUD,
membership, transfer/reclaim, cascade delete, and the last-admin invariant.

## Tombstones (`tombstones.py`)

Server-side façade over the common `TombstoneStore`:

| Function | Line | Role |
| -------- | ---- | ---- |
| `get_tombstone_store` | `tombstones.py` | load/cache the per-bucket store (`buckets` table → folder UUID) |
| `record_deletion` | `:49` | add a tombstone on delete/`deleted` event |
| `clear_deletion` | `:66` | drop a tombstone when the path is re-uploaded |
| `reset_cache` | `:76` | test/lifecycle hook |

TTL bound: `get_tombstone_ttl_days()` (`config.py:472`) → store `prune()`. Causal
merge semantics (vector-clock dominance + content tie-break) and hashed keys are
defined in the common model, not here.

## On-disk layout

```
<buckets_path>/<bucket>/...            # real files (relative paths preserved)
<buckets_path>/<bucket>/.hsynckeep     # sentinel keeping empty dirs alive
<state_dir>/server/<bucket>.db      # versions + tombstones, one per bucket (ADR-0025)
<state_dir>/server/auth.db          # accounts + membership (global)
<state_dir>/.hsyncnodeid               # reserved server node cid
<name>.part                            # in-progress resumable upload
```

Sentinel + cleanup logic: `api/file_utils.py` (`is_sentinel_file:22`,
`ensure_sentinel_in_dir:29`, `cleanup_empty_dir:67`). No `.hsyncfolder` marker
is written into served buckets; the `buckets` table maps name → folder UUID.

> Field-level attribute tables: [common data-models](../../hsync-common/llm-wiki/data-models.md).
