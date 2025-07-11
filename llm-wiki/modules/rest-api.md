# hsync-server — REST API (`api/`)

All paths are prefixed `/api/v1` (`constants.py:38`). Endpoints attach to
`auth_router` (Bearer key) unless noted; `public_router` for health/metrics/time.
Human-oriented flow diagrams: `../docs/architecture.md`; the machine-readable REST
contract is the generated OpenAPI snapshot in `hsync-common` (`protocol/rest/openapi.json`).

## Endpoint map

| Method + path | Handler | Auth | Rate limit |
| ------------- | ------- | ---- | ---------- |
| `GET /buckets/{bucket}` | `buckets.get_bucket` (`buckets.py:42`) | key | default |
| `POST /buckets` | `buckets.create_bucket` (`buckets.py:82`) | key | default |
| `DELETE /buckets/{bucket}` | `buckets.delete_bucket` (`buckets.py:128`) | key | default |
| `GET /buckets/{bucket}/files` | `files/list.get_files` (`list.py:124`) | key | default |
| `GET /buckets/{bucket}/files/upload-status` | `files/list.get_upload_status` (`list.py:40`) | key | default |
| `GET /buckets/{bucket}/tombstones` | `files/list.get_tombstones` (`list.py:194`) | key | default |
| `POST /buckets/{bucket}/files` | `files/upload.upload_file` (`upload.py:70`) | key | upload |
| `GET /files/download` | `files/download.download_file` (`download.py:43`) | key | download |
| `DELETE /files` | `files/delete.delete_file` (`delete.py:44`) | key | default |
| `POST /buckets/{bucket}/tombstones` | tombstone record (`list.py`) | key | default |
| `GET /health` | `health.root` (`health.py:28`) | none | — |
| `GET /api/v1/time` | `health.get_time` (`health.py:45`) | none | — |
| `GET /metrics` | Prometheus (`main.py:127`) | none | — |

Rate-limit values: `RATE_LIMIT_DEFAULT/UPLOAD/DOWNLOAD` (`rate_limiter.py:54`–`60`).

## Buckets (`api/buckets.py`)

- Name validation via `is_valid_bucket` (`api/utils.py:30`) → `InvalidBucketNameError`.
- `create_bucket` mkdir under `get_buckets_path()` with `OWNER_DIRECTORY_ACCESS = 0o700`
  (`api/utils.py:27`); `delete_bucket` uses `shutil.rmtree` (`buckets.py:20`).
- Missing bucket → `BucketNotFoundError` (`exceptions.py:111`).

## Upload (`api/files/upload.py:70`)

Multipart form fields: `cid, file, path, ctime, mtime, sha256sum, permissions`
(default `664`), and resumable fields `offset, size, compressed, compressed_size`
(`upload.py:73`–`83`). Behavior:
- Filename validated/sanitized (`is_valid_filename`/`sanitize_filename` from
  `hsync_common.path_validation`); internal engine names
  (`.hsyncignore`/`.hsyncstate`/`.hsynctombstones`, `*.part`) rejected with
  `InvalidFilenameError`; `cid` format checked with `is_valid_cid`.
- `sha256sum` is a **required** form field and must be a well-formed 64-hex
  digest (`is_sha256_hex`): the received bytes must hash to it
  (`hmac.compare_digest`) or the upload fails with `ChecksumMismatchError`.
- Compressed uploads are **not resumable**: `compressed=true` with
  `offset > 0` is rejected up-front (the resumable path never decompresses).
- Subdir path and filename confined to the bucket via `resolve_within_bucket`
  (`api/utils.py:46`) — rejects cross-bucket escapes (`../other-bucket/x`).
- Resumable: `offset > 0` appends to a `.part` file; full write flushed via
  `_flush_to_disk` → `fsync` (`upload.py:62`). In-flight temp files are
  dot-prefixed (`.hsync-upload-*`), so they never appear in `/files` listings.
- Optional zstd payload expanded with `decompress_file_to_file`
  (`compression.py`, capped via `decompress_stream`) in a worker thread
  (`asyncio.to_thread`): the compressed temp file is decompressed
  chunk-by-chunk into a second temp file which is then renamed over it, so
  neither side is ever fully buffered in memory nor is the event loop
  stalled; the cap is the declared `size` (anti decompression-bomb) and a
  decompressed size different from `size` is rejected.
- On success cache updated `set_sha256_hash` (`hashes.py:28`) and any tombstone
  cleared `clear_deletion` (`tombstones.py:66`).
- Size guard `get_max_upload_size()` (`config.py:397`) → `FileTooLargeError`.
- Permissions applied via `set_permissions` (`api/utils.py:82`) — special bits
  (setuid/setgid/sticky) stripped (`sanitize_remote_permissions`).
- Wire-supplied mtimes clamped to `now + 300 s` (`api/file_utils.py:24`).
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

## Download (`api/files/download.py:43`)

Streams the file; supports zstd via `compression.async_compress_stream`
(`compression.py:53`) when `get_compression_enabled()` (`config.py:437`). The
`filename` is confined with `resolve_within_bucket` (rejects cross-bucket and
internal-engine paths). A file-symlink inside the bucket whose target escapes
it is refused (`is_safe_symlink_target`) — `FileResponse` would otherwise
follow it and serve the out-of-bucket target (e.g. `secret -> /etc/passwd`).
Missing file → `FileNotFoundError` (`exceptions.py:118`).
Accepts an optional `cid` query parameter (sent by the client) that is included
in the `[DOWNLOAD]` log line for request attribution. The log is deferred to a
`BackgroundTasks` callback so it can include the actual transfer duration
(measured from request start to last byte sent) via
`format_duration(..., subsecond=True)` — e.g.
`[DOWNLOAD] "bucket/file" (1500000 bytes) in 5.2s`.

## List / upload-status / tombstones (`api/files/list.py`)

- `get_files` (`list.py:124`) walks the bucket, skips hidden/sentinel/`.part`
  files (`is_hidden_file`, `is_hidden_dir`, `PARTIAL_DOWNLOAD_SUFFIX`), skips
  file-symlinks whose target escapes the bucket (`is_safe_symlink_target`,
  so `secret -> /etc/passwd` never leaks its hash/metadata through the
  listing), attaches cached hashes (`get_sha256_hash`, `hashes.py:42`),
  returns `File` dicts.
- `get_upload_status` (`list.py:40`) reports existing `.part` size for resume.
- `get_tombstones` (`list.py:194`) returns the bucket's tombstone entries from
  `get_tombstone_store` (`tombstones.py:38`); the `POST` counterpart records
  client-side deletions into the same store.

## Delete (`api/files/delete.py:44`)

Removes the file, drops its hash (`del_sha256_hash`, `hashes.py:56`), records a
tombstone (`record_deletion`, `tombstones.py:49`), cleans up now-empty dirs
(`cleanup_empty_dir`, `api/file_utils.py:67`), and broadcasts a `deleted` event.

## Delta Transfer (`api/files/delta/`)

Delta transfer endpoints (rsync-style block sync) for efficient file updates.
See `../docs/delta-transfer.md` for user-facing documentation.

| Method + path | Handler | Auth | Rate limit |
| ------------- | ------- | ---- | ---------- |
| `GET /buckets/{bucket}/files/delta/signatures` | `delta.signatures.get_signatures` (`delta/signatures.py:50`) | key | delta |
| `POST /buckets/{bucket}/files/delta/signatures` | `delta.signatures.match_signatures` (`delta/signatures.py:100`) | key | delta |
| `POST /buckets/{bucket}/files/delta/apply` | `delta.apply.delta_apply` (`delta/apply.py:140`) | key | delta |
| `GET /buckets/{bucket}/files/delta/data/{token}` | `delta.data.get_delta_data` (`delta/data.py:40`) | key | delta |

**Concurrency**: The `delta_apply` endpoint uses a per-file lock manager
(`file_lock_manager.py`) to serialize concurrent delta applies on the same file.
This prevents race conditions where multiple clients would write to the same
`.delta.part` file simultaneously. The lock is acquired before the base file
race check and released after the atomic rename, ensuring that the entire
apply operation is atomic with respect to other delta applies on the same file.

**Caps and limits**:
- `POST /delta/signatures` body size: max 50 MB (`_MAX_SIGNATURES_BODY_SIZE`)
- Total temp file storage: max 10 GB (`DELTA_MAX_TEMP_STORAGE_BYTES`)
- Active tokens: max 1000 (`DELTA_MAX_ACTIVE_TOKENS`)
- Token TTL: 300s (configurable via `HSYNC_DELTA_TOKEN_TTL`)

**Metrics**: All delta endpoints record Prometheus metrics (see `metrics.py`):
transfer counts by status, bytes saved, duration by phase, block change ratios,
active tokens gauge, temp storage gauge. The `delta_apply` success log also
includes the wall-clock duration via `format_duration(..., subsecond=True)`.

## Shared helpers (`api/utils.py`, `api/file_utils.py`)

- `api/utils.py`: bucket validation, `resolve_within_bucket` per-bucket
  confinement (`:46`), permission get/set (`:70`/`:83`/`:103`, wire values
  masked of special bits), `get_dir_info` → `Dir` (`:117`).
- `api/file_utils.py`: sentinel handling (`:27`/`:34`), empty-dir cleanup (`:72`),
  timestamp updates with far-future clamp (`:110`).

> Validation primitives are reused from `hsync_common` (path_validation,
> permissions); do not reimplement them here (DRY rule, `CLAUDE.md`).
