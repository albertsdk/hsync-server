# HSYNC Server — Infrastructure & cross-cutting

Config, security, rate limiting, hashing, compression, errors, logging. Most leaf
helpers are thin wrappers over `hsync_common`; check there first (DRY, `AGENTS.md`).

## Configuration (`config.py`)

- `Settings(BaseSettings)` (`config.py:68`) — pydantic-settings model; all knobs are
  `HSYNC_*` env vars. Singleton via `get_settings()` (`config.py:285`).
- `load_env_file` (`config.py:54`) loads a dotenv before settings resolve.
- Typed accessors (used everywhere instead of touching `Settings` directly):
  `get_port` (`:358`), `get_workers` (`:363`), `get_buckets_path` (`:399`),
  `get_state_dir` (`config.py`),
  `get_admin_key` (`:388`), `get_max_upload_size` (`:460`),
  `get_max_request_body_size` (`:465`; derives `max_upload_size + 64 MiB` when
  `max_request_body_size` is 0; 0 = uncapped),
  `get_compression_enabled` (`:510`), SSL getters (enabled/certfile/keyfile/cert_dir),
  WS getters (`:530`–`:550`), rate-limit getters (`:430`–`:455`),
  `get_tombstone_ttl_days` (`:560`), delta getters (`get_delta_enabled`,
  `get_delta_token_ttl` only — worthwhileness is client-side
  (`hsync_common` constants), so the server has no min-file-size / change-ratio
  knobs).
- Validation sets: `VALID_LOG_LEVELS` (`:34`), `VALID_LOG_FORMATS` (`:43`).
- Admin-key validity (64 lowercase hex) enforced by a field_validator at load
  time; the `lifespan` bootstraps the admin account (`main.py:51`). A leftover
  `HSYNC_API_KEY` in the environment fails startup with a migration message.
- Delta transfer config that **takes effect**: `delta_enabled` (default
  `true`), `delta_token_ttl` (300s), `rate_limit_delta` ("100/minute").

## Authentication (`auth.py` + `auth_store/`)

`verify_api_key` (`auth.py`) — Bearer account token `hs_<id>_<secret>`; scrypt
verify against the account store (`auth_store/`) with a KDF cache (SHA-256 of
the token → `(account_id, generation, key_kind, prior_key_hash)`) and a
per-request DB re-check of status/generation/expiry. A cached **prior** entry is
trusted only while the account's current `prior_key_hash` matches the cached
fingerprint: a later rotation replaces the prior secret, so the entry stops
matching and falls through to a full verify — a rotated-out key cannot survive
the next grace window, while repeated requests within one grace window still
skip scrypt. `key_prefix` (8 hex) is logged instead of the secret. Wired as a
router-level dependency on `auth_router`; `require_admin` gates `admin_router`.
Bucket membership is enforced by `check_bucket_access` (`api/authz.py`).

## Rate limiting (`rate_limiter.py`)

- slowapi `limiter` + `rate_limit_exceeded_handler` (`rate_limiter.py:105`), wired in
  `main.py:86`–`88`.
- Client IP resolution honoring trusted proxies: `get_client_ip` (`:70`),
  `TRUSTED_PROXIES` (`:67` ← `config.get_trusted_proxies` `config.py:392`).
- Limits: `RATE_LIMIT_DEFAULT/UPLOAD/DOWNLOAD/WEBSOCKET` (`:54`–`:63`).
- WS message budget: `TokenBucket` (`:186`), `create_ws_message_bucket` (`:230`),
  parsed from `WS_MESSAGES_DEFAULT` via `_parse_ws_rate` (`:158`).

## SSL / TLS (`ssl_utils.py`)

- The server TLS identity follows the shared service-identity model (ADR-0037):
  **ECDSA P-256**, an **unencrypted** PKCS#8 key (`0600`) in a `0700` directory,
  and a **20-year** validity (`DEFAULT_CERT_VALIDITY_DAYS =
  SERVICE_CERT_VALIDITY_DAYS`). `generate_self_signed_cert` builds on
  `hsync_common.ssl_utils`; the certificate is authenticated by fingerprint
  pinning, not a passphrase, so no key password is configured.
- `ensure_certificates` persists a cert/key pair under `get_ssl_cert_dir()` so the
  fingerprint is stable across restarts (pinning by clients); `get_ssl_config`
  returns the uvicorn SSL kwargs gated by `get_ssl_enabled()`.
- **Two transport topologies** (server TLS is optional):
  (1) behind a TLS-terminating reverse proxy → `get_ssl_enabled()` is `false`, the
  server listens on plain HTTP, and `TRUSTED_PROXIES` gates `X-Forwarded-For`
  (`rate_limiter.py:67`); (2) built-in TLS on a trusted LAN → `get_ssl_enabled()`
  is `true` and uvicorn terminates HTTPS/WSS with the persisted self-signed cert.
  Clients always use `https://`/`wss://` in both cases. User-facing description:
  `../docs/architecture.md` (Transport and TLS).
- `hsync-server admin tls fingerprint` (`__main__.py`, `tls_app`) prints the
  persisted certificate's `sha256:<hex>` (`fingerprint_from_der`,
  `hsync_common.ssl_utils`) for the out-of-band step clients use with
  `hsync trust server add`; `--json` emits `{fingerprint, certificate}`. It reads
  `get_ssl_certfile()` (explicit) or `get_ssl_cert_dir()/server.crt`, and exits
  non-zero when TLS is disabled or the certificate is missing.
- `hsync-server admin tls rotate [--hostname NAME]` regenerates the persisted
  cert/key and prints the new fingerprint (the auto-generated identity only — an
  explicit `HSYNC_SSL_CERTFILE`/`HSYNC_SSL_KEYFILE` is refused). Startup logs a
  warning when the served certificate is within `_CERT_EXPIRY_WARN_DAYS` (30) of
  expiry.

## Hash cache (`hashes.py`)

In-memory unbounded `FILE_HASHES: dict[str, tuple[str, int, int]]` (`hashes.py`)
mapping `sha256(filepath) -> (file_sha256, mtime_ns, size)`. **Not LRU** — no
eviction except stale mtime/size on read. `set_sha256_hash` captures
the current (mtime_ns, size) with the digest; `get_sha256_hash` serves the
digest **only while the file's current metadata matches the captured pair** —
a changed or vanished file drops the stale entry and returns `None`, so
callers (download checksum header, upload dup-skip, delta race check) always
recompute instead of trusting an outdated checksum. `set_sha256_hashes`
(`:73`) walks the buckets tree at startup (called from `main.py:58`) for fast
duplicate detection without recompute. `del_sha256_hash` (`:56`) removes an
entry.

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
`_StreamSink` (`:33`) adapts the zstd writer to an async iterator. The helper
builds **one `ZstdCompressor` per call** — python-zstandard instances are not
safe to share across concurrent/interleaved operations, and stream writes run
on the event-loop thread, so only per-call ownership is correct — a
module-level compressor is forbidden. Construction costs
~1.1 us at level 3 (`bench/bench_compression.py`), negligible against the
>= 10 KB streams this path serves. The one-shot delta-literal path
(`api/files/delta/signatures.py` `_compress_literals`) uses
`hsync_common.compression.compress_data`, whose instances are thread-local.
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
`get_log_level`/`get_log_format`/`get_log_file`/rotation (`config.py:368`–`383`).
Timestamps are rendered in the configured IANA timezone (`get_tz`,
`config.py:378`; `HSYNC_TZ`, default `UTC` — validated by a `field_validator`
on `Settings.tz`) via the tz-aware `timestamp_formatter` passed to
`hsync_common.logger.build_record_formatter` (`logger.py:_build_formatters`).
Console colors are gated by `hsync_common.runtime.color_output_enabled`
(`--no-color` flag, `NO_COLOR` env, stdout TTY). At startup the entry point logs
`hsync_common.runtime.runtime_summary()` (`__main__.py:233`) with the detected
container runtime, cgroup memory/CPU limits and TTY state.
Request correlation via `get_request_id`/`set_request_id` (`:63`/`:68`), set by
`RequestIdMiddleware` (`middleware.py:37`).

`MaxRequestSizeMiddleware` (`middleware.py`) caps every HTTP request body at
`get_max_request_body_size()` bytes *before* FastAPI parses/spools it and
*before* the auth dependency runs: a declared `Content-Length` over the cap is
answered with 413 immediately, and a chunked body is aborted mid-stream (the
internal `_RequestBodyTooLarge` subclasses `BaseException` so FastAPI's
body-parse `except Exception` cannot swallow it into a 400). This is what bounds
what an unauthenticated client can make the server buffer on disk/in memory, the
per-endpoint checks being too late. `0` disables the cap.

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
Used by the upload endpoint (`api/files/upload.py`) and the delta apply endpoint
(`api/files/delta/apply.py`) to serialize the causal arbitration critical
section (read store → decide → write file → record) per path, so concurrent
uploads for the same file are ordered (ADR-0022).

## Version store & arbitration (`versions.py`, `arbitration.py`)

- `versions.py` wraps `hsync_common.models.version.SyncStateStore` per bucket
  in `get_state_dir()/server/<bucket>.db` (bucket id via the `buckets` table,
  ADR-0025). Lazy connection (fork-safe); per-bucket `RLock`.
  `record_version`/`clear_version` maintain the store; `move_version` and
  `move_version_prefix` carry entries across a rename (single file, or every
  file under a renamed directory via a destination-tree walk — the store keys
  are `sha256(relpath)`, so a prefix remap cannot be derived from the store
  itself); `handle_renamed_event` calls them after the on-disk rename.
  `server_version_info` produces the wire `VersionInfo` for listings.
  `get_server_cid` mints and persists a reserved 21-char server node id next
  to the DB for out-of-band edits (ADR-0022 §3.4.7); minting is shared with
  `auth_store.mint_account_id` via `constants.mint_alnum_id` (unbiased
  `secrets.choice`, validated on read with `is_valid_cid`).
- `arbitration.py` exposes `arbitrate_upload` — the shared causal arbitration
  used by both the REST upload and the delta apply endpoint (DRY). Returns an
  `ArbitrationResult` (outcome + record clock + record corrected mtime + loser
  cid for conflict-copy naming). ADR-0023: the tie-break compares the incoming
  `corrected_mtime` with the stored one, falling back to SHA-256 when unknown
  or tied. `conflicted_copy_name` builds the canonical `_conflicted_copy_` path.

## State-DB maintenance (`state_maintenance.py`)

each state DB is compacted in place so pruned version/tombstone pages never
linger (ADR-0025, `auto_vacuum=INCREMENTAL`):

- `vacuum_on_startup()` / `vacuum_on_shutdown()` (`state_maintenance.py`) run a
  full `VACUUM` at the process boundaries via `hsync_common`
  `vacuum_state_db` — safe because no request is in flight. Wired in the
  `lifespan` (`main.py:86`, `main.py:102`). Two deployment guards keep them
  cheap: the startup VACUUM is **gated on `state_db_freelist_count`**
  (`_STARTUP_VACUUM_MIN_FREELIST_PAGES`, skipped on clean restarts), and the
  boundary VACUUM is **single-flight across uvicorn workers** via a
  non-blocking `flock` on a sidecar lock file (`_acquire_vacuum_lock`).
- `run_periodic_incremental_vacuum(stop)` (`state_maintenance.py`) is an
  asyncio task started in the lifespan (`main.py:91`) that runs
  `incremental_vacuum_state_db` (bounded `INCREMENTAL_VACUUM_PAGES`) every
  `INCREMENTAL_VACUUM_INTERVAL_S`, offloaded to a worker thread so the event
  loop never blocks; cancelled on shutdown (`main.py:100`). Idempotent and
  bounded, so one per uvicorn worker is safe.

> User-facing config reference (env vars, defaults): `../docs/` (architecture +
> deployment sections).
