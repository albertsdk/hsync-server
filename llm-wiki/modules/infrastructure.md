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
  `get_tombstone_ttl_days` (`:472`).
- Validation sets: `VALID_LOG_LEVELS` (`:34`), `VALID_LOG_FORMATS` (`:43`).
- API-key validity (presence + min length) enforced by a field_validator at load
  time; the `lifespan` only logs status (`main.py:51`).

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

## Hash cache (`hashes.py`)

In-memory `FILE_HASHES: dict[str, str]` (`hashes.py:25`). `set_sha256_hash` (`:28`),
`get_sha256_hash` (`:42`), `del_sha256_hash` (`:56`); `set_sha256_hashes` (`:73`)
walks the buckets tree at startup (called from `main.py:58`) for fast duplicate
detection without recompute.

## Compression (`compression.py`)

Streaming zstd: `async_compress_stream` (`:53`, 64 KB chunks) for downloads,
`decompress_stream` (`:94`) and `decompress_data` for uploads; `_StreamSink` (`:32`)
adapts the zstd writer to an async iterator. Policy/levels come from
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
Request correlation via `get_request_id`/`set_request_id` (`:63`/`:68`), set by
`RequestIdMiddleware` (`middleware.py:31`).

## Path validation (`path_validation.py`)

Thin re-export/wrapper over `hsync_common.path_validation`
(`is_valid_filename`, `sanitize_filename`, traversal guards) — the server does not
duplicate these primitives.

> User-facing config reference (env vars, defaults): `../docs/` (architecture +
> deployment sections).
