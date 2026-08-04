# hsync-server — Infrastructure & cross-cutting

Config, security, rate limiting, hashing, compression, errors, logging. Most leaf
helpers are thin wrappers over `hsync_common`; check there first (DRY, `CLAUDE.md`).

## Configuration (`config.py`)

- `Settings(BaseSettings)` (`config.py:68`) — pydantic-settings model; all knobs are
  `HSYNC_*` env vars. Singleton via `get_settings()` (`config.py:285`).
- `load_env_file` (`config.py:54`) loads a dotenv before settings resolve.
- Typed accessors (used everywhere instead of touching `Settings` directly):
  `get_port` (`:317`), `get_workers` (`:322`), `get_buckets_path` (`:362`),
  `get_api_key` (`:357`), `get_max_upload_size` (`:397`),
  `get_compression_enabled` (`:437`), SSL getters (`:412`–`:432`),
  WS getters (`:442`–`:462`), rate-limit getters (`:367`–`:392`),
  `get_tombstone_ttl_days` (`:472`), delta getters (`get_delta_enabled`,
  `get_delta_min_file_size`, `get_delta_max_change_ratio`, `get_delta_token_ttl`).
- Validation sets: `VALID_LOG_LEVELS` (`:34`), `VALID_LOG_FORMATS` (`:43`).
- API-key validity (presence + min length) enforced by a field_validator at load
  time; the `lifespan` only logs status (`main.py:51`).
- Delta transfer config: `delta_enabled` (default `true`), `delta_min_file_size`
  (256 KB), `delta_max_change_ratio` (0.50 = 50%), `delta_token_ttl` (300s),
  `rate_limit_delta` ("100/minute").

## Authentication (`routers.py`)

`verify_api_key` (`routers.py:45`) — Bearer token, `hmac.compare_digest`
(`routers.py:69`) to avoid timing attacks; `mask_api_key` (`routers.py:27`) for safe
logging. Wired as a router-level dependency on `auth_router`.

## Rate limiting (`rate_limiter.py`)

- slowapi `limiter` + `rate_limit_exceeded_handler` (`rate_limiter.py:105`), wired in
  `main.py:86`–`88`.
- Client IP resolution honoring trusted proxies: `get_client_ip` (`:70`),
  `TRUSTED_PROXIES` (`:67` ← `config.get_trusted_proxies` `config.py:392`).
- Limits: `RATE_LIMIT_DEFAULT/UPLOAD/DOWNLOAD/WEBSOCKET` (`:54`–`:63`).
- WS message budget: `TokenBucket` (`:186`), `create_ws_message_bucket` (`:230`),
  parsed from `WS_MESSAGES_DEFAULT` via `_parse_ws_rate` (`:158`).

## SSL / TLS (`ssl_utils.py`)

- `generate_self_signed_cert` (`:46`, validity `DEFAULT_CERT_VALIDITY_DAYS = 365`
  `:43`) builds on `hsync_common.ssl_utils`.
- `ensure_certificates` (`:138`) persists a cert/key pair under
  `get_ssl_cert_dir()` (`config.py:432`) so the fingerprint is stable across
  restarts (TOFU pinning by peers); `get_ssl_config` (`:172`) returns the uvicorn
  SSL kwargs gated by `get_ssl_enabled()` (`config.py:412`).
- **Two transport topologies** (server TLS is optional):
  (1) behind a TLS-terminating reverse proxy → `get_ssl_enabled()` is `false`, the
  server listens on plain HTTP, and `TRUSTED_PROXIES` gates `X-Forwarded-For`
  (`rate_limiter.py:67`); (2) built-in TLS on a trusted LAN → `get_ssl_enabled()`
  is `true` and uvicorn terminates HTTPS/WSS with the persisted self-signed cert.
  Clients always use `https://`/`wss://` in both cases. User-facing description:
  `../docs/architecture.md` (Transport and TLS).

## Hash cache (`hashes.py`)

In-memory `FILE_HASHES: dict[str, str]` (`hashes.py:25`). `set_sha256_hash` (`:28`),
`get_sha256_hash` (`:42`), `del_sha256_hash` (`:56`); `set_sha256_hashes` (`:73`)
walks the buckets tree at startup (called from `main.py:58`) for fast duplicate
detection without recompute.

## Prometheus metrics (`metrics.py`)

Delta transfer operations expose Prometheus metrics for monitoring:

- `hsync_delta_transfer_total`: Counter for delta transfer attempts (labels:
  `direction` = upload/download, `status` = success/failure/rejected/conflict)
- `hsync_delta_bytes_saved`: Counter for bytes saved by delta transfer vs full
  transfer (label: `direction`)
- `hsync_delta_transfer_duration_seconds`: Histogram of transfer duration by phase
  (labels: `direction`, `phase` = signatures/match/apply/data/total)
- `hsync_delta_block_change_ratio`: Histogram of block change ratios (label:
  `direction`) — helps identify when files are too different for delta
- `hsync_delta_active_tokens`: Gauge for currently active download tokens
- `hsync_delta_temp_storage_bytes`: Gauge for total bytes used by temp files

Metrics are recorded in the delta endpoints (`signatures.py`, `apply.py`, `data.py`,
`tokens.py`) and exposed via the `/metrics` endpoint (when prometheus_client is
available). Helper functions: `record_delta_transfer`, `record_bytes_saved`,
`record_transfer_duration`, `record_change_ratio`, `update_active_tokens`,
`update_temp_storage`.

## Compression (`compression.py`)

Streaming zstd: `async_compress_stream` (`:54`, 64 KB chunks) for downloads;
`_StreamSink` (`:33`) adapts the zstd writer to an async iterator.
Decompression: `decompress_file_to_file` (`:95`, capped streaming
file→file, fsync'd — run it via `asyncio.to_thread`, used by uploads so
neither the payload nor the event loop is blocked) plus the
`hsync_common.compression` primitives (`decompress_stream` capped streaming,
`decompress_data` one-shot capped). Policy/levels come from
`hsync_common.compression`.

## Errors (`error_handlers.py` + `exceptions.py`)

- Domain tree rooted at `HSyncError` (`exceptions.py:25`, extends the common base):
  `ValidationError` (`:43`) → bucket/path/permission/checksum/size/filename errors;
  `ResourceNotFoundError` (`:104`) → bucket/file; `AuthenticationError` (`:130`) →
  missing/invalid key; `FileOperationError` (`:156`) → upload/download/delete/storage.
- `register_exception_handlers` (`error_handlers.py:166`) installs:
  `hsync_exception_handler` (`:84`) using `get_status_code_for_exception` (`:65`) +
  `EXCEPTION_STATUS_CODES` map (`:53`); `validation_exception_handler` (`:112`);
  `global_exception_handler` (`:140`). All emit the `ErrorResponse` model (`:37`).

## Logging (`logger.py`)

`InterceptHandler` (`logger.py:73`) extends the common `BaseInterceptHandler` to
route stdlib logging into loguru. `setup_logging` (`:103`) honors
`get_log_level`/`get_log_format`/`get_log_file`/rotation (`config.py:332`–`347`).
Console colors are gated by `hsync_common.runtime.color_output_enabled`
(`--no-color` flag, `NO_COLOR` env, stdout TTY). At startup the entry point logs
`hsync_common.runtime.runtime_summary()` (`__main__.py:233`) with the detected
container runtime, cgroup memory/CPU limits and TTY state.
Request correlation via `get_request_id`/`set_request_id` (`:63`/`:68`), set by
`RequestIdMiddleware` (`middleware.py:31`).

**Action-label colorization**: the server has no project-local label table.
`ActionLabel` is re-exported from `hsync_common.labels`
(`hsync_server/constants.py:23`), and the formatter's `message_transform`
calls `hsync_common.labels.colorize_labels` (`logger.py:98`) so the server's
log lines are colorized identically to the client's. REST/WS handlers emit
labels via `ActionLabel.*` (e.g. `[UPLOAD]`/`[DOWNLOAD]` in `api/files/`,
`[WS]` infra messages in `api/events.py` + `api/connection_manager.py`,
`[CREATE]`/`[MODIFY]`/`[DELETE]`/`[RENAME]`/`[SYMLINK]`/`[SECURITY]` in the
WS event `handlers.py`).

## Path validation

All primitives come straight from `hsync_common.path_validation`
(`is_valid_filename`, `sanitize_filename`, `validate_remote_relpath`,
`resolve_within`, `is_internal_name`) — the server does not duplicate or wrap
them (the old `path_validation.py` shim has been removed). Per-bucket
confinement goes through `resolve_within_bucket` (`api/utils.py:46`).

## File lock manager (`file_lock_manager.py`)

Singleton manager for per-file `asyncio.Lock` instances used to serialize
concurrent operations on the same file path. The `FileLockManager` class
(`file_lock_manager.py:36`) maintains a dictionary mapping file paths to Lock
objects, created on-demand and reused. The `acquire` async context manager
(`file_lock_manager.py:56`) ensures that only one coroutine can execute a
critical section for a given file path at a time.

The singleton is accessed via `get_file_lock_manager()` (`file_lock_manager.py:86`).
Currently used by the delta apply endpoint (`api/files/delta/apply.py`) to prevent
race conditions when multiple clients attempt concurrent delta applies on the same
file (which would otherwise write to the same `.delta.part` file simultaneously).

> User-facing config reference (env vars, defaults): `../docs/` (architecture +
> deployment sections).
