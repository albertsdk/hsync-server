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
- Filename validated/sanitized (`is_valid_filename`/`sanitize_filename`,
  `path_validation.py`); subdir path checked against the bucket root.
- Resumable: `offset > 0` appends to a `.part` file; full write flushed via
  `_flush_to_disk` → `fsync` (`upload.py:62`).
- Optional zstd payload decompressed with `decompress_data` (`compression.py`).
- SHA-256 verified against `sha256sum` → `ChecksumMismatchError` (`exceptions.py:78`);
  on success cache updated `set_sha256_hash` (`hashes.py:28`) and any tombstone
  cleared `clear_deletion` (`tombstones.py:66`).
- Size guard `get_max_upload_size()` (`config.py:397`) → `FileTooLargeError`.
- Permissions applied via `set_permissions` (`api/utils.py:77`).
- Broadcasts an `added` event to bucket subscribers (see
  [websocket.md](websocket.md)).

## Download (`api/files/download.py:43`)

Streams the file; supports zstd via `compression.async_compress_stream`
(`compression.py:53`) when `get_compression_enabled()` (`config.py:437`). Missing
file → `FileNotFoundError` (`exceptions.py:118`).

## List / upload-status / tombstones (`api/files/list.py`)

- `get_files` (`list.py:124`) walks the bucket, skips hidden/sentinel/`.part`
  files (`is_hidden_file`, `is_hidden_dir`, `PARTIAL_DOWNLOAD_SUFFIX`), attaches
  cached hashes (`get_sha256_hash`, `hashes.py:42`), returns `File` dicts.
- `get_upload_status` (`list.py:40`) reports existing `.part` size for resume.
- `get_tombstones` (`list.py:194`) returns the bucket's tombstone entries from
  `get_tombstone_store` (`tombstones.py:38`); the `POST` counterpart records
  client-side deletions into the same store.

## Delete (`api/files/delete.py:44`)

Removes the file, drops its hash (`del_sha256_hash`, `hashes.py:56`), records a
tombstone (`record_deletion`, `tombstones.py:49`), cleans up now-empty dirs
(`cleanup_empty_dir`, `api/file_utils.py:67`), and broadcasts a `deleted` event.

## Shared helpers (`api/utils.py`, `api/file_utils.py`)

- `api/utils.py`: bucket/dir validation, permission get/set (`:64`/`:77`/`:94`),
  `get_dir_info` → `Dir` (`:107`).
- `api/file_utils.py`: sentinel handling (`:22`/`:29`), empty-dir cleanup (`:67`),
  timestamp updates (`:105`).

> Validation primitives are reused from `hsync_common` (path_validation,
> permissions); do not reimplement them here (DRY rule, `CLAUDE.md`).
