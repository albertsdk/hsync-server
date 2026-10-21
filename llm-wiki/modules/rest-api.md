# HSYNC Server — REST API (`api/`)

All paths are prefixed `/api/v1` from the shared SSOT
(`hsync_common/protocol/rest.py::REST_API_PREFIX`). Endpoints attach to
`auth_router` (Bearer key) unless noted; `public_router` for health/metrics/time.
Human-oriented flow diagrams: `../docs/internal/architecture.md`; the machine-readable REST
contract is the generated OpenAPI snapshot in `hsync-common` (`protocol/rest/openapi.json`).

The route decorators do not hard-code literals: they use the shared path names in
`hsync_common.protocol.rest` (`REST_*` constants, `rest_bucket_template`,
`rest_delta_data_path`, `REST_BUCKET_EVENTS_PATH_TEMPLATE`). `REST_PATH_TEMPLATES`
is asserted **byte-for-byte** against the committed snapshot by
`hsync-common/tests/test_protocol_rest.py`, so the server, the snapshot and the
client mirrors cannot drift.

## Endpoint map

| Method + path | Handler | Auth | Rate limit |
| ------------- | ------- | ---- | ---------- |
| `GET /buckets/{bucket}` | `buckets.get_bucket` (`api/buckets.py::get_bucket`) | key | default |
| `POST /buckets` | `buckets.create_bucket` (`api/buckets.py::create_bucket`) | key | default |
| `DELETE /buckets/{bucket}` | `buckets.delete_bucket` (`api/buckets.py::delete_bucket`) | key | default |
| `GET /buckets/{bucket}/files` | `files/list.get_files` (`api/files/list.py::get_files`) | key | default |
| `GET /buckets/{bucket}/files/upload-status` | `files/list.get_upload_status` (`api/files/list.py::get_upload_status`) | key | default |
| `GET /buckets/{bucket}/tombstones` | `files/list.get_tombstones` (`api/files/list.py::get_tombstones`) | key | default |
| `POST /buckets/{bucket}/files` | `files/upload.upload_file` (`api/files/upload.py::upload_file`) | key | upload |
| `GET /files/download` | `files/download.download_file` (`api/files/download.py::download_file`) | key | download |
| `DELETE /files` | `files/delete.delete_file` (`api/files/delete.py::delete_file`) | key | default |
| `POST /buckets/{bucket}/tombstones` | `files/list.post_tombstones` (`api/files/list.py::post_tombstones`) | key | default |
| `GET /buckets/{bucket}/presence` | `presence.get_presence` (`api/presence.py::get_presence`) | key | default |
| `GET /health` | `health.root` (`api/health.py::root`) | none | — |
| `GET /api/v1/time` | `health.get_time` (`api/health.py::get_time`) | none | — |
| `GET /metrics` | Prometheus (`main.py::app`) | none | — |

`GET /health` accepts an optional `size` query param (bounded to
`HEALTH_MAX_PROBE_BYTES`, `api/health.py::HEALTH_MAX_PROBE_BYTES`): it returns that many NUL bytes so
the client's autotune can measure throughput (`measure_bandwidth_active`).
Default (no `size`) is the empty 200 readiness probe.

Rate-limit values: `config.py::get_rate_limit_default` /
`config.py::get_rate_limit_upload` / `config.py::get_rate_limit_download`.

## Buckets (`api/buckets.py` + `bucket_provisioning.py`)

- Name validation via `is_valid_bucket_name` (shared SSOT,
  `hsync_common.path_validation`) → `InvalidBucketNameError`.
- Filesystem + ownership logic lives in `bucket_provisioning.py`, the single
  path shared by the REST endpoint AND the local admin CLI
  (`admin bucket create` / `admin bucket delete`, `__main__.py`):
  - `create_bucket` (`bucket_provisioning.py::create_bucket`) mkdir under
    `get_buckets_path()` + `AuthStore.set_owner_on_create`; on ownership
    failure the orphan directory is removed before raising `DeleteError`.
    Existing name → `ConflictError` (REST maps it to 409). The admin
    create-on-behalf endpoint (`api/admin.py`) delegates here too, so it
    inherits the same name validation and orphan rollback.
  - `delete_bucket` (`bucket_provisioning.py::delete_bucket`) validates the name, then
    delegates to `AuthStore.delete_bucket` (DB rows + `shutil.rmtree`). With
    `delete_files=False` it calls `AuthStore.delete_bucket_db` only — the CLI
    exposes it as `admin bucket delete --bucket <name> --db-only` to clean up
    "ghost" rows whose directory no longer exists.
- `buckets.py` only translates outcomes to HTTP: create catches
  `ConflictError` → 409, delete re-raises for the standard handler mapping
  (the REST endpoint never uses `delete_files=False`). The REST delete runs the
  blocking `delete_bucket` (whole-tree `shutil.rmtree` + DB rows) on a worker
  thread (`asyncio.to_thread`, `api/buckets.py::delete_bucket`) so a large bucket cannot stall
  the event loop; the admin CLI path stays synchronous (it has no loop to free).
  A concurrent delete that loses the race (directory already gone when the
  removal runs) maps to 404, not 500 (`bucket_provisioning.py::delete_bucket`).
- **Test isolation net**: `tests/conftest.py` adds autouse
  `no_tests_touch_real_repo_state`, which snapshots the repository's real
  `state/server/` and `buckets/` around each test and fails the
  offending test on any mutation (protection against test rows leaking into
  the developer's real DB).
- Missing bucket → `BucketNotFoundError` (`exceptions.py::BucketNotFoundError`).

## Upload (`api/files/upload.py::upload_file`)

Multipart form fields: `cid, file, path, ctime, mtime, sha256sum, permissions`
(default `664`), resumable fields `offset, size, compressed, compressed_size`,
and causal version fields `vclock` (JSON-encoded vector clock), `version_id`
(ADR-0022) and `corrected_mtime` (ADR-0023; the uploader's offset-corrected
modification time, 0 ⇒ unknown ⇒ hash fallback). Behavior:
- Filename validated/sanitized (`is_valid_filename`/`sanitize_filename` from
  `hsync_common.path_validation`); internal engine names
  (`.hsyncignore`/`.hsyncfolder`/legacy `.hsyncstate`/`.hsynctombstones`, `*.part`) rejected with
  `InvalidFilenameError`; `cid` format checked with `is_valid_cid`.
- `sha256sum` is a **required** form field and must be a well-formed 64-hex
  digest (`is_sha256_hex`): the received bytes must hash to it
  (`hmac.compare_digest`) or the upload fails with `ChecksumMismatchError`.
- **Causal arbitration** (`arbitration.py`): the incoming version is compared
  against the stored version for the path (inside the per-file
  `FileLockManager`). Outcomes: `accepted`/`accepted_conflict_won` (201 with
  `resolution` and a `version` object carrying the recorded merged `vclock`
  and `corrected_mtime`), `superseded` (409, stale ancestor, no copy),
  `conflict_lost` (409, incoming loses tie-break, bytes preserved as
  conflicted copy). The chronological tie-break (ADR-0023) uses the
  corrected mtimes: higher wins, missing/tie falls back to SHA-256.
- Compressed uploads are **not resumable**: `compressed=true` with
  `offset > 0` is rejected up-front (the resumable path never decompresses).
- Subdir path and filename confined to the bucket via `resolve_within_bucket`
  (`api/utils.py::resolve_within_bucket`) — rejects cross-bucket escapes (`../other-bucket/x`).
- Resumable: `offset > 0` appends to a `.part` file; full write flushed via
  `_flush_to_disk` → `fsync` (`api/files/upload.py::_flush_to_disk`). In-flight temp files are
  dot-prefixed (`.hsync-upload-*`), so they never appear in `/files` listings.
- **Overwrite backup**: when the target already exists, its previous content is
  snapshotted to `<path>.backup` by `_backup_existing_file` (`api/files/upload.py::_backup_existing_file`) — a
  hard link (O(1), same inode) with a `copy2` fallback when the filesystem has
  no link support or a stale backup blocks the link. The publish uses an atomic
  `replace` onto a new inode, so the snapshot never aliases the new content.
  The snapshot is only a **fallback** for the download endpoint when the primary
  is absent (see the download section below): normal downloads serve the primary,
  which the atomic publish keeps whole, and preferring the transient backup was a
  TOCTOU that could 500 when the uploader removed it mid-request.
- Optional zstd payload expanded with `decompress_file_to_file`
  (`compression.py::decompress_file_to_file`, capped via `decompress_stream`) in a worker thread
  (`asyncio.to_thread`): the compressed temp file is decompressed
  chunk-by-chunk into a second temp file which is then renamed over it, so
  neither side is ever fully buffered in memory nor is the event loop
  stalled; the cap is the declared `size` (anti decompression-bomb) and a
  decompressed size different from `size` is rejected.
- On success the post-publish bookkeeping is shared with delta apply through
  `api/files/version_publish.py::record_published_version`: invalidate then
  repopulate the SHA-256 cache (`hashes.py::set_sha256_hash`), clear the
  deletion marker (`tombstones.py::clear_deletion`), record the causal version
  and capture the published model for the WebSocket broadcast. The helper keeps
  the cache order canonical (invalidate before set) for both endpoints.
- Size guard `get_max_upload_size()` (`config.py::get_max_upload_size`) → `FileTooLargeError`.
- **Same-SHA metadata branch** (`api/files/upload.py::upload_file`): when the uploaded content
  hash equals the on-disk hash, the server does NOT reject the upload — it
  updates the file timestamp from the incoming `ctime`/`mtime`, then compares
  the incoming permissions against the stored file and, on a divergence,
  applies the new mode (`set_permissions`, sanitized) and re-broadcasts a
  `modified` event to the other connected clients, responding
  `200 {"info": "Permissions updated"}` (no byte rewrite, no version bump).
  Identical content with no metadata change keeps the legacy
  `409 File already present`. The metadata apply + `modified` re-broadcast is
  the shared `apply_metadata_to_existing_file` helper
  (`api/files/permissions.py::apply_metadata_to_existing_file`), also used by the dedicated `PATCH` endpoint.
- **`PATCH …/files/permissions`** (`api/files/permissions.py::patch_file_permissions`):
  metadata-only convergence (chmod and/or touch).
  Query params `filename` (bucket-relative), `permissions` (octal, sanitized),
  optional `mtime` (ADR-0023 corrected Unix timestamp), `vclock` (JSON-encoded
  effective vector clock) and `version_id`. Applies whatever diverges WITHOUT
  any file payload, records the change as a real causal version
  (`record_version`, so listings and future events advertise the new metadata
  instead of the peer re-detecting the divergence every sync) and re-broadcasts
  a causal `modified` event; `200 {"info": "Metadata updated"}` on change (or
  `"info": "Metadata already current"` when identical), `404` when the file
  does not exist, `400` on invalid permissions/path/name/vclock. Used by the
  client as the first-choice path for content-unchanged metadata uploads so a
  chmod or a plain `touch` on a large file never re-sends its bytes. Applying
  an mtime preserves the cached content digest so the next same-SHA upload
  still hits the dedup skip.
- **`GET …/dirs`** (`api/files/list.py::get_dirs`): directory metadata
  listing `[{"path": <bucket-relative>, "permissions": <octal>}, …]`, sorted
  by path; skips hidden dirs and dir-symlinks. Directories carry no content
  hash and never appear in `/files`, so this listing feeds the clients'
  download-only directory-permission converge.
- Permissions applied via `set_permissions` (`api/utils.py::set_permissions`) — special bits
  (setuid/setgid/sticky) stripped (`sanitize_remote_permissions`).
- Wire-supplied mtimes clamped to `now + 300 s` (`api/file_utils.py::MTIME_MAX_FUTURE_SKEW_SECONDS`).
- Broadcasts an `added` event to bucket subscribers, skipping hidden files
  (see [websocket.md](websocket.md)).
- **No path leak on errors**: error responses never expose absolute
  filesystem paths. The 409 "file already present" returns a bucket-relative
  path; `UploadError.details` carries only the exception type name (not
  `str(e)`, which typically embeds absolute paths); the resume-missing-partial
  error no longer includes the `partial_file` field. Full diagnostics remain
  in the server log (`log.error(..., exc_info=True)`).
- **Duration logging**: the `[UPLOAD]` success log includes the wall-clock
  duration via `format_duration(..., subsecond=True)` — e.g.
  `[UPLOAD] "bucket/path/file" (CID: abc) in 1.2s`.

## Download (`api/files/download.py::download_file`)

Streams the file; supports zstd via `compression.async_compress_stream`
(`compression.py::async_compress_stream`) when `get_compression_enabled()`
(`config.py::get_compression_enabled`); the
compression itself runs on a dedicated worker pool (see
`llm-wiki/modules/infrastructure.md#compression-compressionpy`), not the event
loop. The `filename` is confined with `resolve_within_bucket` (full resolve): a
symlink whose target stays inside the bucket is served **transparently**
(static-server semantics — this endpoint serves content on demand), while one
whose target escapes the bucket fails the confinement and is rejected (400).
Symlinks are *replicated* structurally via the WS `symlink` event and
`POST /symlinks`, never through this content endpoint. Missing file →
`FileNotFoundError` (`exceptions.py::FileNotFoundError`).

Readers always receive a **complete** file: the primary is served (every writer
publishes through an atomic `replace`, so a concurrent overwrite is observed as
the old or the new inode, never a partial one — the session returns the complete
previous version while an upload is in flight). The transient `<path>.backup`
snapshot is used only when the primary is absent, never preferred: picking it
first was a TOCTOU where the uploader could remove it between the existence
check and the stat / checksum / response and turn a benign concurrent overwrite
into a 500. A file removed between the stat and the checksum read maps to the
same `FileNotFoundError` (404).

> **Coding contract**: the response body carries no HTTP content coding other
> than `identity` or `Content-Encoding: zstd` (ADR 0010). The route is excluded
> from `TransferAwareGZipMiddleware` (`middleware.py::TransferAwareGZipMiddleware`), so a client that
> advertises gzip still receives the exact bytes with an ordinary
> `Content-Length` — clients abort a transfer on any other coding, and chunked
> gzip would invalidate the `Range`/resume offsets. The same exclusion applies
> to the delta literal payload (`files/delta/data/{token}`).

Accepts an optional `cid` query parameter (sent by the client) that is included
in the `[DOWNLOAD]` log line for request attribution. The log is deferred to a
`BackgroundTasks` callback so it can include the actual transfer duration
(measured from request start to last byte sent) via
`format_duration(..., subsecond=True)` — e.g.
`[DOWNLOAD] "bucket/file" (1500000 bytes) in 5.2s`.

## Presence (`api/presence.py`)

`GET /buckets/{bucket}/presence` returns the display-ready `{connected, total}`
pair for a bucket: `total` counts the non-admin bucket members
(`AuthStore.get_share_member_ids`), `connected` counts those members with a
live WebSocket on this worker (`BucketConnectionManager.presence_counts`). The
same pair is broadcast as a `presence` WebSocket frame on every join/leave
(see [websocket.md](websocket.md)) and, in p2p mode, advertised by the master
through the `connected` TXT value. Access follows `check_bucket_access` (404
when the bucket does not exist or the caller is not a member).

## List / upload-status / tombstones (`api/files/list.py`)

The `/files` listing resolves per-file metadata (`File.get`, `hsync_common/models/file.py::File.get`)
on **asyncio worker threads** (`asyncio.to_thread`): a cache-miss recomputes
the SHA-256 (blocking I/O + CPU), so the per-file hashing no longer blocks the
loop. The directory walk itself, hidden/symlink checks and the per-entry
version-store join still run on the loop (bounded, no hashing).

`File.get` offloads elsewhere, and where the flow stays on the loop **on
purpose**: upload (`upload.py`) and delta apply (`apply.py`) capture their
broadcast metadata on a worker thread **inside the per-path FileLockManager
section**, so the broadcast describes exactly the generation that upload
published (no lock released → no same-path interleaving). The pre-delete hash
(`delete.py`) and the permission-only convergence read (`permissions.py`)
remain synchronous: offloading them would open an event-loop window in which a
same-path upload could mix generations into the tombstone/event metadata —
correctness over loop-freedom.


- `get_files` (`api/files/list.py::get_files`) walks the bucket, skips hidden/sentinel/`.part`
  files (`is_hidden_file`, `is_hidden_dir`, `PARTIAL_DOWNLOAD_SUFFIX`), skips
  file-symlinks whose target escapes the bucket (`is_safe_symlink_target`,
  so `secret -> /etc/passwd` never leaks its hash/metadata through the
  listing), attaches cached hashes (`get_sha256_hash`, `hashes.py::get_sha256_hash`),
  returns `File` dicts.
- `get_upload_status` (`api/files/list.py::get_upload_status`) reports existing `.part` size for resume.
  **p2p parity**: the same endpoint is now served by p2p masters — Python
  (`hsync_client/p2p_server.py::_handle_upload_status`) and Android
  (`P2PServer.kt`, `handleUploadStatus`) — so the client resume logic works
  uniformly in both server mode and P2P mode.
- `get_tombstones` (`api/files/list.py::get_tombstones`) returns the bucket's tombstone entries from
  `get_tombstone_store` (`tombstones.py::get_tombstone_store`); the `POST` counterpart records
  client-side deletions into the same store. Its body is streamed with a 50 MB
  cap (`read_request_body_capped`, `api/utils.py::read_request_body_capped`) so a hostile member cannot
  buffer an unbounded body; an oversized body is a 400.

## Delete (`api/files/delete.py::delete_file`)

Removes the file (for a symlink path, the **link itself**, never its target —
final component left unresolved by the confinement), drops its hash
(`del_sha256_hash`, `hashes.py::del_sha256_hash`), records a tombstone (`record_deletion`,
`tombstones.py::record_deletion`), cleans up now-empty dirs (`cleanup_empty_dir`,
`api/file_utils.py::cleanup_empty_dir`), and broadcasts a `deleted` event. The delta apply
endpoint (`delta/apply.py`) **refuses symlink paths with 400**: a delta
patches file content and must never turn a symlink into a regular file.

## Delta Transfer (`api/files/delta/`)

Delta transfer endpoints (rsync-style block sync) for efficient file updates.
See `../docs/delta-transfer.md` for user-facing documentation.

| Method + path | Handler | Auth | Rate limit |
| ------------- | ------- | ---- | ---------- |
| `GET /buckets/{bucket}/files/delta/signatures` | `delta.signatures.get_delta_signatures` (`api/files/delta/signatures.py::get_delta_signatures`) | key | delta |
| `POST /buckets/{bucket}/files/delta/signatures` | `delta.signatures.post_delta_signatures` (`api/files/delta/signatures.py::post_delta_signatures`) | key | delta |
| `POST /buckets/{bucket}/files/delta/apply` | `delta.apply.delta_apply` (`api/files/delta/apply.py::delta_apply`) | key | delta |
| `GET /buckets/{bucket}/files/delta/data/{token}` | `delta.data.get_delta_data` (`api/files/delta/data.py::get_delta_data`) | key | delta |

**Concurrency**: The `delta_apply` endpoint uses a per-file lock manager
(`file_lock_manager.py`) to serialize concurrent delta applies on the same file.
Signature computation and matching (`compute_signatures`, `find_matches_file`),
literal compression and token issuance all run on **asyncio worker threads**
(`asyncio.to_thread`) so the event loop never blocks. Two shared caches are
cross-thread by design: `_signature_cache` (`api/files/delta/signatures.py::_signature_cache`, TTL 300 s,
FIFO cap 64, guarded by `_signature_cache_lock` — computed outside the lock)
and `delta_token_store` (`api/files/delta/tokens.py::delta_token_store`, `threading.RLock` around
issue/lookup/consume/discard/cleanup so the token-cap and storage checks are
atomic). Regression coverage: `tests/test_concurrency_hardening.py`.
This prevents race conditions where multiple clients would write to the same
`.delta.part` file simultaneously. The lock is acquired before the base file
race check and released after the atomic rename, ensuring that the entire
apply operation is atomic with respect to other delta applies on the same file.
Publishing a rebuilt version reuses the same
`api/files/version_publish.py::record_published_version` bookkeeping as the
full-upload endpoint (cache invalidation order, tombstone clearing, causal
version, broadcast model capture).

**Caps and limits**:
- `POST /delta/signatures` body size: max 50 MB (`_MAX_SIGNATURES_BODY_SIZE`,
  `api/files/delta/signatures.py::_MAX_SIGNATURES_BODY_SIZE`),
  enforced by streaming (`read_request_body_capped`) so the body is never fully
  buffered first.
- `POST /delta/apply` result size: bounded by `get_max_upload_size()` and the
  same free-space reserve as a full upload (shared `enforce_size_cap` +
  `ensure_disk_space`, `api/utils.py::enforce_size_cap`). An over-cap `new_size` is a 400
  (`FileTooLargeError`), and a reserve breach is a 507 (`InsufficientStorageError`);
  otherwise N copy ops (each within `base_size`) could declare an arbitrarily
  large `new_size` and amplify a small request into a disk-filling write.
- Global request body: `MaxRequestSizeMiddleware` (`middleware.py::MaxRequestSizeMiddleware`) rejects any
  HTTP body over `get_max_request_body_size()` with 413 at the ASGI boundary
  (before parsing and before auth); see `infrastructure.md`.
- Total temp file storage: max 10 GB (`DELTA_MAX_TEMP_STORAGE_BYTES`,
  `api/files/delta/tokens.py::DELTA_MAX_TEMP_STORAGE_BYTES`)
- Active tokens: max 1000 (`DELTA_MAX_ACTIVE_TOKENS`,
  `api/files/delta/tokens.py::DELTA_MAX_ACTIVE_TOKENS`)
- Token TTL: 300s (configurable via `HSYNC_DELTA_TOKEN_TTL`)

**Metrics**: All delta endpoints record Prometheus metrics (see `metrics.py`):
transfer counts by status, bytes saved, duration by phase, block change ratios,
active tokens gauge, temp storage gauge. The `delta_apply` success log also
includes the wall-clock duration via `format_duration(..., subsecond=True)`.

## Shared helpers (`api/utils.py`, `api/file_utils.py`, `api/files/version_publish.py`)

- `api/utils.py`: bucket validation, `resolve_within_bucket` per-bucket
  confinement (`api/utils.py::resolve_within_bucket`), permission get/set
  (`api/utils.py::get_permissions` / `api/utils.py::set_permissions` / `api/utils.py::set_directory_permissions`,
  wire values masked of special bits).
- `api/file_utils.py`: sentinel handling (`api/file_utils.py::is_sentinel_file` /
  `api/file_utils.py::ensure_sentinel_in_dir`), empty-dir cleanup (`api/file_utils.py::cleanup_empty_dir`),
  timestamp updates with far-future clamp (`api/file_utils.py::update_file_timestamp`).
- `api/files/version_publish.py`: `record_published_version` — shared post-publish
  bookkeeping (hash cache, deletion marker, causal version, broadcast model)
  used by full upload and delta apply.

> Validation primitives are reused from `hsync_common` (path_validation,
> permissions); do not reimplement them here (DRY rule, `AGENTS.md`).
