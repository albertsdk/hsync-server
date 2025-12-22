# HSYNC Server — Data & state

The server owns **no** sync data types — those come from `hsync_common.models`
(see [common data-models](../../hsync-common/llm-wiki/data-models.md)). It only adds
process-local state and side-car persistence.

## Reused common models

| Type | Used by | Source |
| ---- | ------- | ------ |
| `File` / `FileDict` | list, upload, download responses | `hsync_common.models.file` |
| `Dir` | `get_dir_info` (`api/utils.py:107`) | `hsync_common.models.dir` |
| `Symlink` | symlink event handler | `hsync_common.models.symlink` |
| `Event` / `EventName` | WS broadcast + handlers | `hsync_common.models.event` |
| `TombstoneStore` / `TombstoneEntry` | deletion tracking | `hsync_common.models.tombstone` |

## Server-local state

| State | Location | Lifetime | Persistence |
| ----- | -------- | -------- | ----------- |
| SHA-256 cache `FILE_HASHES` | `hashes.py:25` | process memory | rebuilt at startup from disk (`hashes.py:73`) |
| Per-bucket WS pools | `connection_manager.py:166` (`bucket_manager`) | process memory | none (live connections) |
| Tombstone stores (per bucket) | `tombstones.py` cache | process memory + side-car | `.hsynctombstones` file |

## Tombstones (`tombstones.py`)

Server-side façade over the common `TombstoneStore`:

| Function | Line | Role |
| -------- | ---- | ---- |
| `_store_path` | `tombstones.py:33` | locate the `.hsynctombstones` side-car for a bucket |
| `get_tombstone_store` | `:38` | load/cache the per-bucket store |
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
<buckets_path>/<bucket>/.hsynctombstones
<name>.part                            # in-progress resumable upload
```

Sentinel + cleanup logic: `api/file_utils.py` (`is_sentinel_file:22`,
`ensure_sentinel_in_dir:29`, `cleanup_empty_dir:67`). Side-car filenames are the
shared constants from `hsync_common.constants`.

> Field-level attribute tables: [common data-models](../../hsync-common/llm-wiki/data-models.md).
