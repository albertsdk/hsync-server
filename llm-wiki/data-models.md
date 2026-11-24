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
| SHA-256 cache `FILE_HASHES` | `hashes.py` | process memory (bounded LRU, `hsync_server/hashes.py::FILE_HASHES_MAX_SIZE`) | rebuilt at startup from disk (`set_sha256_hashes`); LRU-evicted past the cap; installed as hsync-common's `File.get` provider so listings share it |
| Delta signature cache `_signature_cache` | `api/files/delta/signatures.py` | process memory (TTL 300 s, FIFO cap 64, `Lock`-guarded) | none (recomputed on demand) |
| Delta literal-payload tokens `delta_token_store` | `api/files/delta/tokens.py` | process memory (`RLock`-guarded) + payload temp files | none (TTL-reaped) |
| Per-bucket WS pools | `hsync_server/api/connection_manager.py::bucket_manager` | process memory | none (live connections) |
| Tombstone stores (per bucket) | `tombstones.py` cache | process memory + SQLite | per-bucket DB `server/<bucket>.db` (`tombstones` table) |
| Version stores (per bucket) | `versions.py` cache | process memory + SQLite | per-bucket DB `server/<bucket>.db` (`versions` table) |
| Accounts + membership | `auth_store/` | SQLite | global `server/auth.db` (`clients`, `bucket_members` tables) |

The SHA-256 cache maps `sha256(filepath) -> (file_sha256, mtime_ns, ctime_ns, size)`: a
cached digest is served only while the file's current metadata matches the
captured pair, otherwise the entry is dropped and callers recompute (see
[modules/infrastructure.md](modules/infrastructure.md) — Hash cache).

## Accounts and membership (`auth_store/`)

Server-only tables on the global auth DB `server/auth.db` (not part of the
common `sqlstore` schema):

- **`clients`** — `id` (PK, 21-alnum), `name` (UNIQUE, immutable), `role`
  (`admin`/`client`), `status` (`active`/`disabled`), `key_hash` (PHC scrypt),
  `key_prefix` (8 hex for logs), `key_generation`, `key_expires_at`,
  `prior_key_hash`/`prior_expires_at` (grace rotation), `created_at`,
  `last_used_at`.
- **`bucket_members`** — PK `(bucket, client_id)`, `role` (`owner`/`member`);
  partial UNIQUE index enforces exactly one owner per bucket.

`AuthStore` (`hsync_server/auth_store/store.py::AuthStore`) is the single source of truth for account CRUD,
membership, transfer/reclaim, cascade delete, and the last-admin invariant.

The built-in admin's id (`hsync_server/auth_store/credentials.py::BUILTIN_ADMIN_ID`)
is persisted in `clients.id`, embedded in its token and referenced by
`bucket_members.client_id`; it is never minted by
`credentials.py::mint_account_id`. Renaming it orphans an existing store:
`accounts.py::AccountsMixin.bootstrap_admin` then reports "accounts but no
built-in admin" and `__main__.py::bucket_create` cannot resolve the owner.

## Tombstones (`tombstones.py`)

Server-side façade over the common `TombstoneStore`:

| Function | Location | Role |
| -------- | -------- | ---- |
| `get_tombstone_store` | `hsync_server/tombstones.py::get_tombstone_store` | load/cache the per-bucket store (`buckets` table → folder UUID) |
| `record_deletion` | `hsync_server/tombstones.py::record_deletion` | add a tombstone on delete/`deleted` event |
| `clear_deletion` | `hsync_server/tombstones.py::clear_deletion` | drop a tombstone when the path is re-uploaded |
| `deletion_marker_snapshot` | `hsync_server/tombstones.py::deletion_marker_snapshot` | opaque `(version_id, vclock)` snapshot of a path's marker (`None` when absent); changes on every mint/clear |
| `destination_mutation_since` | `hsync_server/tombstones.py::destination_mutation_since` | commit gate: true when *abs_path* was removed or replaced since a snapshot — presence check for delete/rename-away, marker comparison for delete-and-recreate |
| `reset_cache` | `hsync_server/tombstones.py::reset_cache` | test/lifecycle hook |

TTL bound: `get_tombstone_ttl_days()` (`hsync_server/config.py::get_tombstone_ttl_days`) → store `prune()`. Causal
merge semantics (vector-clock dominance + content tie-break) and hashed keys are
defined in the common model, not here.

The two helpers above implement the **pre-commit destination re-validation**
shared with the client's P2P master: `hsync_server/api/files/upload.py::upload_file` and
`hsync_server/api/files/delta/apply.py::delta_apply` snapshot the destination
(`existed_at_start` + marker) before the body streams and re-check it right
before the atomic `replace`. A destination deleted, renamed away or
deleted-and-recreated while the body ran is answered `409 destination_changed`
and the temp artifact is discarded — the commit can never resurrect a removed
name or clobber a recreated file.

## History store (`history.py`, ADR-0057)

The server keeps one blob per path, so the content an overwrite replaces or a
delete removes would be lost. `hsync_server/history.py` preserves those bytes per
bucket under `<state dir>/history`, outside every served bucket — no peer sees
them, the wire protocol is unchanged.

| Symbol | Role |
| --- | --- |
| `capture_file` | link-or-copy a regular file into a new entry (`reason` = `overwrite` \| `delete`), write `entry.json` atomically, return the entry id (or `None` on any failure — a recovery copy never aborts the operation) |
| `entries` | the bucket's entries, newest first, skipping a damaged record |
| `prune` | retention by age (`retention_days`) then the oldest over `max_bytes`; the byte budget counts the recorded payload, not the record |

Contract and helpers live in `hsync-common`:
`hsync-common/hsync_common/models/history.py::HistoryEntry`,
`::parse_history_entry`, `hsync-common/hsync_common/models/history.py::history_entry_dir`.
Bounded by `HISTORY_RETENTION_DAYS_DEFAULT` / `HISTORY_QUOTA_PERCENT_DEFAULT` /
`HISTORY_QUOTA_FLOOR_MB_DEFAULT`
(`hsync-common/hsync_common/defaults.py`).

Capture is hooked into `hsync_server/maintenance.py::delete_path_state` (before the
tombstone and the unlink) and into both publish paths
(`hsync_server/api/files/upload.py`, `hsync_server/api/files/delta/apply.py`,
before the atomic `replace`), so an overwrite and a deletion both leave a
recoverable copy.

## On-disk layout

```
<buckets_path>/<bucket>/...            # real files (relative paths preserved)
<buckets_path>/<bucket>/.hsynckeep     # sentinel keeping empty dirs alive
<state_dir>/server/<bucket>.db      # versions + tombstones, one per bucket (ADR-0025)
<state_dir>/server/auth.db          # accounts + membership (global)
<state_dir>/.hsyncnodeid               # reserved server node cid
<state_dir>/history/<bucket>/<id>/...   # preserved bytes, ADR-0057
<name>.part                            # in-progress resumable upload
```

Sentinel + cleanup logic: `api/file_utils.py` (`is_sentinel_file`
`hsync-common/hsync_common/models/sentinel.py::is_sentinel_file`, `ensure_sentinel_in_dir`
`hsync_server/api/file_utils.py::ensure_sentinel_in_dir`, `cleanup_empty_dir`
`hsync_server/api/file_utils.py::cleanup_empty_dir`). No `.hsyncfolder` marker
is written into served buckets; the `buckets` table maps name → folder UUID.

> Field-level attribute tables: [common data-models](../../hsync-common/llm-wiki/data-models.md).
