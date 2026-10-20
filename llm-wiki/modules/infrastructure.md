# HSYNC Server — Infrastructure & cross-cutting

Config, security, rate limiting, hashing, compression, errors, logging. Most leaf
helpers are thin wrappers over `hsync_common`; check there first (DRY, `AGENTS.md`).

## Configuration (`config.py`)

- `Settings(BaseSettings)` (`config.py::Settings`) — pydantic-settings model; all knobs are
  `HSYNC_*` env vars. Singleton via `get_settings()` (`config.py::get_settings`).
- `load_env_file` (`config.py::load_env_file`) loads a dotenv before settings resolve.
- Typed accessors (used everywhere instead of touching `Settings` directly):
  `get_port` (`config.py::get_port`), `get_workers` (`config.py::get_workers`),
  `get_buckets_path` (`config.py::get_buckets_path`),
  `get_state_dir` (`config.py::get_state_dir`),
  `get_admin_key` (`config.py::get_admin_key`), `get_max_upload_size` (`config.py::get_max_upload_size`),
  `get_max_request_body_size` (`config.py::get_max_request_body_size`; derives `max_upload_size + 64 MiB` when
  `max_request_body_size` is 0; 0 = uncapped),
  `get_compression_enabled` (`config.py::get_compression_enabled`), SSL getters
  (`config.py::get_ssl_enabled` / `::get_ssl_certfile` / `::get_ssl_keyfile` / `::get_ssl_cert_dir`),
  WS getters (`config.py::get_ws_auth_timeout` / `::get_ws_heartbeat_interval` /
  `::get_ws_heartbeat_timeout` / `::get_ws_max_connections` / `::get_ws_max_connections_per_ip`),
  rate-limit getters (`config.py::get_rate_limit_default` / `::get_rate_limit_upload` /
  `::get_rate_limit_download` / `::get_rate_limit_ws_messages` / `::get_rate_limit_admin` / `::get_rate_limit_delta`),
  `get_tombstone_ttl_days` (`config.py::get_tombstone_ttl_days`), delta getters (`config.py::get_delta_enabled`,
  `config.py::get_delta_token_ttl` only — worthwhileness is client-side
  (`hsync_common` constants), so the server has no min-file-size / change-ratio
  knobs).
- Validation sets: `VALID_LOG_LEVELS` (`config.py::VALID_LOG_LEVELS`), `VALID_LOG_FORMATS` (`config.py::VALID_LOG_FORMATS`).
- Admin-key validity (64 lowercase hex) enforced by a field_validator at load
  time; the `lifespan` bootstraps the admin account (`main.py::lifespan`). A leftover
  `HSYNC_API_KEY` in the environment fails startup with a migration message.
- Delta transfer config that **takes effect**: `delta_enabled` (default
  `true`), `delta_token_ttl` (300s), `rate_limit_delta` ("100/minute").

## Authentication (`auth.py` + `auth_store/`)

`verify_api_key` (`auth.py::verify_api_key`) — Bearer account token `hs_<id>_<secret>`; scrypt
verify against the account store (`auth_store/`) with a KDF cache (SHA-256 of
the token → `(account_id, generation, key_kind, prior_key_hash)`) and a
per-request DB re-check of status/generation/expiry. A cached **prior** entry is
trusted only while the account's current `prior_key_hash` matches the cached
fingerprint: a later rotation replaces the prior secret, so the entry stops
matching and falls through to a full verify — a rotated-out key cannot survive
the next grace window, while repeated requests within one grace window still
skip scrypt. `key_prefix` (8 hex) is logged instead of the secret. Wired as a
router-level dependency on `auth_router`; `require_admin` (`auth.py::require_admin`) gates `admin_router`.
Bucket membership is enforced by `check_bucket_access` (`api/authz.py::check_bucket_access`).

## Rate limiting (`rate_limiter.py`)

- slowapi `limiter` + `rate_limit_exceeded_handler` (`rate_limiter.py::rate_limit_exceeded_handler`), wired in
  `main.py::app`.
- Client IP resolution honoring trusted proxies: `get_client_ip` (`rate_limiter.py::get_client_ip`),
  `TRUSTED_PROXIES` (`rate_limiter.py::TRUSTED_PROXIES` ← `config.get_trusted_proxies` `config.py::get_trusted_proxies`).
- Limits: `config.py::get_rate_limit_default` / `::get_rate_limit_upload` /
  `::get_rate_limit_download` / `::get_rate_limit_ws_messages`.
- WS message budget: `TokenBucket` (`rate_limiter.py::TokenBucket`), `create_ws_message_bucket` (`rate_limiter.py::create_ws_message_bucket`),
  parsed from `WS_MESSAGES_DEFAULT` via `_parse_ws_rate` (`rate_limiter.py::_parse_ws_rate`).

## SSL / TLS (`ssl_utils.py`)

- The server TLS identity follows the shared service-identity model (ADR-0037):
  **ECDSA P-256**, an **unencrypted** PKCS#8 key (`0600`) in a `0700` directory,
  and a **20-year** validity (`DEFAULT_CERT_VALIDITY_DAYS =
  SERVICE_CERT_VALIDITY_DAYS`, `ssl_utils.py::DEFAULT_CERT_VALIDITY_DAYS`). `generate_self_signed_cert`
  (`ssl_utils.py::generate_self_signed_cert`) builds on
  `hsync_common.ssl_utils`; the certificate is authenticated by fingerprint
  pinning, not a passphrase, so no key password is configured.
- `ensure_certificates` (`ssl_utils.py::ensure_certificates`) persists a cert/key pair under `get_ssl_cert_dir()` so the
  fingerprint is stable across restarts (pinning by clients); `get_ssl_config`
  (`ssl_utils.py::get_ssl_config`) returns the uvicorn SSL kwargs gated by `get_ssl_enabled()`.
- **Two transport topologies** (server TLS is optional):
  (1) behind a TLS-terminating reverse proxy → `get_ssl_enabled()` is `false`, the
  server listens on plain HTTP, and `TRUSTED_PROXIES` gates `X-Forwarded-For`
  (`rate_limiter.py::TRUSTED_PROXIES`); (2) built-in TLS on a trusted LAN → `get_ssl_enabled()`
  is `true` and uvicorn terminates HTTPS/WSS with the persisted self-signed cert.
  Clients always use `https://`/`wss://` in both cases. User-facing description:
  `../docs/internal/architecture.md` (Transport and TLS).
- `hsync-server admin tls fingerprint` (`__main__.py`, `tls_app`) prints the
  persisted certificate's `sha256:<hex>` (`fingerprint_from_der`,
  `hsync_common.ssl_utils`) for the out-of-band step clients use with
  `hsync trust server add`; `--json` emits `{fingerprint, certificate}`. It reads
  `get_ssl_certfile()` (explicit) or `get_ssl_cert_dir()/server.crt`, and exits
  non-zero when TLS is disabled or the certificate is missing.
- `hsync-server admin tls rotate [--hostname NAME]` regenerates the persisted
  cert/key and prints the new fingerprint (the auto-generated identity only — an
  explicit `HSYNC_SSL_CERTFILE`/`HSYNC_SSL_KEYFILE` is refused). Startup logs a
  warning when the served certificate is within `_CERT_EXPIRY_WARN_DAYS` (30,
  `__main__.py::_CERT_EXPIRY_WARN_DAYS`) of expiry.

## Hash cache (`hashes.py`)

In-memory unbounded `FILE_HASHES: dict[str, tuple[str, int, int]]` (`hashes.py::FILE_HASHES`)
mapping `sha256(filepath) -> (file_sha256, mtime_ns, size)`. **Not LRU** — no
eviction except stale mtime/size on read. `set_sha256_hash` (`hashes.py::set_sha256_hash`) captures
the current (mtime_ns, size) with the digest; `get_sha256_hash` (`hashes.py::get_sha256_hash`) serves the
digest **only while the file's current metadata matches the captured pair** —
a changed or vanished file drops the stale entry and returns `None`, so
callers (download checksum header, upload dup-skip, delta race check) always
recompute instead of trusting an outdated checksum. `set_sha256_hashes`
(`hashes.py::set_sha256_hashes`) walks the buckets tree at startup (called from `main.py::lifespan`) for fast
duplicate detection without recompute. `del_sha256_hash` (`hashes.py::del_sha256_hash`) removes an
entry.

## Prometheus metrics (`metrics.py`)

Delta transfer operations expose Prometheus metrics for monitoring:

- `hsync_delta_transfer_total` (`metrics.py::hsync_delta_transfer_total`): Counter for delta transfer attempts (labels:
  `direction` = upload/download, `status` = success/failure/rejected/conflict)
- `hsync_delta_bytes_saved` (`metrics.py::hsync_delta_bytes_saved`): Counter for bytes saved by delta transfer vs full
  transfer (label: `direction`)
- `hsync_delta_transfer_duration_seconds` (`metrics.py::hsync_delta_transfer_duration_seconds`): Histogram of transfer duration by phase
  (labels: `direction`, `phase` = signatures/match/apply/data/total)
- `hsync_delta_block_change_ratio` (`metrics.py::hsync_delta_block_change_ratio`): Histogram of block change ratios (label:
  `direction`) — helps identify when files are too different for delta
- `hsync_delta_active_tokens` (`metrics.py::hsync_delta_active_tokens`): Gauge for currently active download tokens
- `hsync_delta_temp_storage_bytes` (`metrics.py::hsync_delta_temp_storage_bytes`): Gauge for total bytes used by temp files

Metrics are recorded in the delta endpoints (`signatures.py`, `apply.py`, `data.py`,
`tokens.py`) and exposed via the `/metrics` endpoint (when prometheus_client is
available). Helper functions: `record_delta_transfer` (`metrics.py::record_delta_transfer`), `record_bytes_saved`
(`metrics.py::record_bytes_saved`), `record_transfer_duration` (`metrics.py::record_transfer_duration`), `record_change_ratio`
(`metrics.py::record_change_ratio`), `update_active_tokens` (`metrics.py::update_active_tokens`), `update_temp_storage`
(`metrics.py::update_temp_storage`).

## Compression (`compression.py`)

Compressed downloads run **off the event loop**. `async_compress_stream`
(`compression.py::async_compress_stream`) reads the file on the loop via `aiofiles`, coalesces reads into
`_COMPRESS_BATCH_BYTES` (~1 MiB, `compression.py::_COMPRESS_BATCH_BYTES`) batches and compresses each batch on a
dedicated worker pool (`_COMPRESS_MAX_WORKERS`, `compression.py::_COMPRESS_MAX_WORKERS`, resolved from the cgroup
CPU quota clamped by `os.cpu_count()`; never the
shared asyncio default executor that hashing/decompression use). The batch size
is kept at 1 MiB on purpose: a larger batch removes a few offload hops but the
consumer receives nothing until the whole batch is compressed, and a sweep
measured first-byte latency growing steeply with the batch. A worker is
held only while a batch is compressed, **never while the consumer is parked on
socket backpressure**, so a small CPU-sized pool serves many concurrent — even
slow — downloads with no starvation. `_StreamSink` (`compression.py::_StreamSink`) buffers the writer
output and is drained (and yielded) on the loop after each batch. Each call owns
its `ZstdCompressor` for the whole transfer (python-zstandard instances are not
safe to share across concurrent/interleaved operations — a module-level
compressor is forbidden); `write`/`close` are always awaited one at a time, so
the pool may run them on different threads but never concurrently. Construction,
writes and the finalizing `close()` all run on the pool; the single-frame header
embeds the uncompressed size so one-shot client/Android decompression still
works, and memory is bounded to one batch per transfer. Construction costs
~1.1 us at level 3 (`bench/bench_compression.py`), negligible against the
>= 10 KB streams this path serves. The one-shot delta-literal path
(`api/files/delta/signatures.py::_compress_literals`) uses
`hsync_common.compression.compress_data`, whose instances are thread-local.
Decompression: `decompress_file_to_file` (`compression.py::decompress_file_to_file`, capped streaming
file→file, fsync'd — run it via `asyncio.to_thread`, used by uploads so
neither the payload nor the event loop is blocked) plus the
`hsync_common.compression` primitives (`decompress_stream` capped streaming,
`decompress_data` one-shot capped). Policy/levels come from
`hsync_common.compression`.

## Errors (`error_handlers.py` + `exceptions.py`)

- Domain tree rooted at `HSyncError` (`exceptions.py::HSyncError`, extends the common base):
  `ValidationError` (`exceptions.py::ValidationError`) → bucket/path/permission/checksum/size/filename errors;
  `ResourceNotFoundError` (`exceptions.py::ResourceNotFoundError`) → bucket/file; `AuthenticationError`
  (`exceptions.py::AuthenticationError`) → missing/invalid key; `FileOperationError` (`exceptions.py::FileOperationError`) →
  upload/download/delete/storage.
- `register_exception_handlers` (`error_handlers.py::register_exception_handlers`) installs:
  `hsync_exception_handler` (`error_handlers.py::hsync_exception_handler`) using `get_status_code_for_exception` (`error_handlers.py::get_status_code_for_exception`) +
  `EXCEPTION_STATUS_CODES` map (`error_handlers.py::EXCEPTION_STATUS_CODES`); `validation_exception_handler` (`error_handlers.py::validation_exception_handler`);
  `global_exception_handler` (`error_handlers.py::global_exception_handler`). All emit a uniform JSON error
  envelope (`error_handlers.py::hsync_exception_handler`).

## Logging (`logger.py`)

`InterceptHandler` (`logger.py::InterceptHandler`) extends the common `BaseInterceptHandler` to
route stdlib logging into loguru. `setup_logging` (`logger.py::setup_logging`) honors
`get_log_level`/`get_log_format`/`get_log_file`/rotation (`config.py::get_log_level` /
`config.py::get_log_format` / `config.py::get_log_file`).
Timestamps are rendered in the configured IANA timezone (`get_tz`,
`config.py::get_tz`; `HSYNC_TZ`, default `UTC` — validated by a `field_validator`
on `Settings.tz`) via the tz-aware formatter built in `_build_formatters`
(`logger.py::_build_formatters`) and passed to
`hsync_common.logger.build_record_formatter`.
Console colors are gated by `hsync_common.runtime.color_output_enabled` with the
`--color` mode (`auto`: TTY + `NO_COLOR`; `always`; `never`). At startup the entry
point logs
`hsync_common.runtime.runtime_summary()` (`hsync_common/runtime.py::runtime_summary`) with the detected
container runtime, cgroup memory/CPU limits and TTY state.
Request correlation via `get_request_id`/`set_request_id` (`logger.py::get_request_id` /
`logger.py::set_request_id`), set by
`RequestIdMiddleware` (`middleware.py::RequestIdMiddleware`).

`MaxRequestSizeMiddleware` (`middleware.py::MaxRequestSizeMiddleware`) caps every HTTP request body at
`get_max_request_body_size()` bytes *before* FastAPI parses/spools it and
*before* the auth dependency runs: a declared `Content-Length` over the cap is
answered with 413 immediately, and a chunked body is aborted mid-stream (the
internal `_RequestBodyTooLarge` (`middleware.py::_RequestBodyTooLarge`) subclasses `BaseException` so FastAPI's
body-parse `except Exception` cannot swallow it into a 400). This is what bounds
what an unauthenticated client can make the server buffer on disk/in memory, the
per-endpoint checks being too late. `0` disables the cap.

**Action-label colorization**: the server has no project-local label table.
`ActionLabel` is re-exported from `hsync_common.labels`
(`hsync_common/labels.py::ActionLabel`), and the formatter's `message_transform`
calls `hsync_common.labels.colorize_labels` (via `logger.py::_transform_colored`) so the server's
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
confinement goes through `resolve_within_bucket` (`api/utils.py::resolve_within_bucket`).

## File lock manager (`file_lock_manager.py`)

Singleton manager for per-file `asyncio.Lock` instances used to serialize
concurrent operations on the same file path. The `FileLockManager` class
(`file_lock_manager.py::FileLockManager`) maintains a dictionary mapping file paths to Lock
objects, created on-demand and reused. The `acquire` async context manager
(`file_lock_manager.py::FileLockManager.acquire`) ensures that only one coroutine can execute a
critical section for a given file path at a time.

The singleton is accessed via `get_file_lock_manager()` (`file_lock_manager.py::get_file_lock_manager`).
Used by the upload endpoint (`api/files/upload.py`) and the delta apply endpoint
(`api/files/delta/apply.py`) to serialize the causal arbitration critical
section (read store → decide → write file → record) per path, so concurrent
uploads for the same file are ordered (ADR-0022).

## Version store & arbitration (`versions.py`, `arbitration.py`)

- `versions.py` wraps `hsync_common.models.version.SyncStateStore` per bucket
  in `get_state_dir()/server/<bucket>.db` (bucket id via the `buckets` table,
  ADR-0025). Lazy connection (fork-safe); per-bucket `RLock`.
  `record_version`/`clear_version` maintain the store; `move_version` (`versions.py::move_version`) and
  `move_version_prefix` (`versions.py::move_version_prefix`) carry entries across a rename (single file, or every
  file under a renamed directory via a destination-tree walk — the store keys
  are `sha256(relpath)`, so a prefix remap cannot be derived from the store
  itself); `handle_renamed_event` calls them after the on-disk rename.
  `server_version_info` (`versions.py::server_version_info`) produces the wire `VersionInfo` for listings.
  `get_server_cid` (`versions.py::get_server_cid`) mints and persists a reserved 21-char server node id next
  to the DB for out-of-band edits (ADR-0022 §3.4.7); minting is shared with
  `auth_store.mint_account_id` via `constants.mint_alnum_id` (`constants.py::mint_alnum_id`; unbiased
  `secrets.choice`, validated on read with `is_valid_cid`).
- `arbitration.py` exposes `arbitrate_upload` (`arbitration.py::arbitrate_upload`) — the shared causal arbitration
  used by both the REST upload and the delta apply endpoint (DRY). Returns an
  `ArbitrationResult` (`arbitration.py::ArbitrationResult`; outcome + record clock + record corrected mtime + loser
  cid for conflict-copy naming). ADR-0023: the tie-break compares the incoming
  `corrected_mtime` with the stored one, falling back to SHA-256 when unknown
  or tied. `conflicted_copy_name` (`arbitration.py::conflicted_copy_name`) builds the canonical `_conflicted_copy_` path.

## State-DB maintenance (`state_maintenance.py`)

each state DB is compacted in place so pruned version/tombstone pages never
linger (ADR-0025, `auto_vacuum=INCREMENTAL`):

- `vacuum_on_startup()` / `vacuum_on_shutdown()` (`state_maintenance.py::vacuum_on_startup` / `state_maintenance.py::vacuum_on_shutdown`) run a
  full `VACUUM` at the process boundaries via `hsync_common`
  `vacuum_state_db` — safe because no request is in flight. Wired in the
  `lifespan` (`main.py::lifespan`). Two deployment guards keep them
  cheap: the startup VACUUM is **gated on `state_db_freelist_count`**
  (`_STARTUP_VACUUM_MIN_FREELIST_PAGES`, `state_maintenance.py::_STARTUP_VACUUM_MIN_FREELIST_PAGES`, skipped on clean restarts), and the
  boundary VACUUM is **single-flight across uvicorn workers** via a
  non-blocking `flock` on a sidecar lock file (`_acquire_vacuum_lock`,
  `state_maintenance.py::_acquire_vacuum_lock`).
- `run_periodic_incremental_vacuum(stop)` (`state_maintenance.py::run_periodic_incremental_vacuum`) is an
  asyncio task started in the lifespan (`main.py::lifespan`) that runs
  `incremental_vacuum_state_db` (bounded `INCREMENTAL_VACUUM_PAGES`) every
  `INCREMENTAL_VACUUM_INTERVAL_S`, offloaded to a worker thread so the event
  loop never blocks; cancelled on shutdown (`main.py::lifespan`). Idempotent and
  bounded, so one per uvicorn worker is safe.

> User-facing config reference (env vars, defaults): `../docs/` (architecture +
> deployment sections).
