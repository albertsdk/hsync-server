# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation
- **Transport/TLS model clarified and made consistent**: documentation now
  states uniformly that the server speaks HTTP/HTTPS with optional TLS in two
  topologies — behind a TLS-terminating reverse proxy (recommended for
  Internet-facing servers) or with the built-in persistent self-signed
  certificate (trusted private LAN) — while clients always use `https://`/`wss://`.
  Aligned the self-signed certificate wording across `docs/faq.md` and
  `docs/security-best-practices.md`, added a "Transport and TLS" section to
  `docs/architecture.md`, documented the healthcheck scheme caveat when built-in
  TLS is enabled in `docs/deployment.md`, and recorded the two topologies in
  `llm-wiki/modules/infrastructure.md`.

### Added
- **Runtime environment awareness**: at startup the server logs a one-line
  summary of the detected runtime environment (container runtime, cgroup
  memory/CPU limits, TTY state) via `hsync_common.runtime.runtime_summary()`.
  Colored output now also honors the standard [`NO_COLOR`](https://no-color.org)
  environment variable, in addition to `--no-color` and the automatic non-TTY
  detection.
- **`install.sh --version latest`**: the installer now accepts `latest` as the
  version value, resolving the newest stable GitHub Release automatically
  (via the `releases/latest` redirect; drafts and pre-releases are excluded)
  and pinning the resolved `vX.Y.Z` image tag in `.env`, so users no longer
  need to know a release tag in advance. The getting-started guide now points
  at `releases/latest/download/install.sh` for a version-free bootstrap.
- **Canonical Compose installer**: release assets now include a version-pinned,
  checksum-published `install.sh` and `compose.yaml`. The idempotent bootstrap
  generates local API/TLS secrets, persists buckets and the self-signed LAN
  certificate in named volumes, validates Compose, and waits for server health.
- **Single getting-started path**: `docs/getting-started.md` is now the canonical
  private-LAN quick start; source, direct-Docker, Poetry, and systemd methods are
  organized separately by use case.
- **Delta transfer endpoints**: four new REST endpoints for rsync-style block
  synchronization (`/api/v1/buckets/{bucket}/files/delta/`): `GET /signatures`
  (fetch block signatures of a file), `POST /signatures` (match client signatures
  and return delta instructions + data URL), `POST /apply` (apply computed delta
  to rebuild file), `GET /data/{token}` (download literal payload). Delta transfer
  reduces bandwidth by transferring only changed blocks for large files with small
  changes. Includes signature cache, token management (single-use, TTL 300s),
  per-file upload lock, rate limiting (`HSYNC_DELTA_RATE_LIMIT`), and feature flag
  (`HSYNC_DELTA_ENABLED`).
- **Delta transfer configuration**: new config fields `delta_min_file_size`
  (default 256 KB) and `delta_max_change_ratio` (default 0.50 = 50%) to control
  when delta transfer is attempted. Configurable via `HSYNC_DELTA_MIN_FILE_SIZE`
  and `HSYNC_DELTA_MAX_CHANGE_RATIO` environment variables.
- **Delta transfer caps**: hard limits to prevent resource exhaustion:
  - POST `/delta/signatures` body size: max 50 MB (`_MAX_SIGNATURES_BODY_SIZE`)
  - Total temp file storage: max 10 GB (`DELTA_MAX_TEMP_STORAGE_BYTES`)
  - Active tokens: max 1000 (`DELTA_MAX_ACTIVE_TOKENS`)
- **Prometheus metrics for delta transfer**: comprehensive monitoring metrics
  exposed via `/metrics` endpoint:
  - `hsync_delta_transfer_total`: Counter for transfer attempts by status
  - `hsync_delta_bytes_saved`: Counter for bandwidth savings
  - `hsync_delta_transfer_duration_seconds`: Histogram of transfer duration by phase
  - `hsync_delta_block_change_ratio`: Histogram of block change ratios
  - `hsync_delta_active_tokens`: Gauge for active download tokens
  - `hsync_delta_temp_storage_bytes`: Gauge for temp storage usage

### Fixed
- **Delta apply concurrency**: concurrent delta apply operations on the same
  file are now serialized using a per-file lock manager (`file_lock_manager.py`).
  This prevents race conditions where multiple clients would write to the same
  `.delta.part` file simultaneously, which could cause file corruption. The lock
  is acquired before the base file race check and released after the atomic
  rename, ensuring the entire apply operation is atomic with respect to other
  delta applies on the same file.

### Security
- **Path leak on error responses**: upload error responses no longer
  expose absolute filesystem paths. The 409 "file already present" returns a
  bucket-relative path; `UploadError.details` exposes only the exception
  type (not `str(e)`, which commonly embeds absolute paths); the
  `partial_file` field was removed from the resume-error details. Full
  diagnostics remain in the server log.
- **Upload integrity (breaking)**: `sha256sum` is now a required multipart
  form field — uploads without a checksum, or whose bytes do not hash to it,
  are rejected (OpenAPI snapshot regenerated accordingly)
- **Per-bucket confinement**: new `resolve_within_bucket` confines every
  client-supplied path to the *target* bucket (upload/download/delete/list),
  closing the cross-bucket escape (`bucket=A`, `path=../B/x`) left open by
  the old root-wide `is_valid_dir` check; backslashes, absolute paths and
  internal engine files are rejected as well
- **Internal engine filenames rejected in upload**:
  `.hsyncignore`/`.hsyncstate`/`.hsynctombstones` (and `*.part`) return 400
- **Decompression-bomb protection**: compressed uploads are decompressed with
  a hard cap (the declared `size`) and the real decompressed size must match
  the declaration. Decompression streams chunk-by-chunk from temp file to
  temp file in a worker thread (`asyncio.to_thread`), so a large expanded
  payload no longer sits in server memory nor stalls the event loop
- **WebSocket event hardening**: every event `src`/`dst` path is validated
  (`validate_remote_relpath`) before dispatch or relay, dropping traversal,
  absolute and backslash payloads; when a payload carries both `filepath` and
  `path`, *all* of them must validate; connection CIDs are format-validated
- **Permissions hardening**: setuid/setgid/sticky bits are stripped from
  client-supplied permission strings before being applied
- **Constant-time checksum comparisons**: the upload SHA-256 integrity check
  (and the duplicate-detection comparison) now use `hmac.compare_digest`;
  declared checksums are format-validated first (`is_sha256_hex`), since
  `compare_digest` raises `TypeError` on non-ASCII input
- **WebSocket bucket validation**: the `{bucket}` URL segment is now
  validated with `is_valid_bucket` at accept time — a segment like `..`
  (reachable via `%2e%2e`) previously escaped the storage root and turned
  delete/rename/symlink events into arbitrary sibling-directory operations
- **WebSocket broadcast robustness**: delivery iterates a snapshot of the
  connections and tolerates dead sockets (evicting them) instead of aborting
  mid-broadcast or raising on concurrent connect/disconnect
- **Duplicate-CID eviction**: a second WebSocket connection reusing an
  existing cid now explicitly evicts the stale registration instead of
  silently hijacking it, and disconnect cleanup only removes the registration
  if it still belongs to the closing socket
- **Compressed uploads are not resumable**: `compressed=true` with
  `offset > 0` is rejected up-front (400) — the resumable path never
  decompresses, so such uploads could only fail integrity later
- **In-flight upload temp files are hidden** (dot-prefixed), so partially
  written content no longer appears in `/files` listings where peers could
  sync it under random names
- **Timestamp hardening**: far-future client-supplied mtimes are clamped to
  `now + 5 min` (conflict-engineering mitigation)

### Removed
- **`hsync_server.path_validation` shim**: import the validation primitives
  directly from `hsync_common.path_validation`; the duplicated
  `is_safe_symlink_target` in `api/events_handlers/validation.py` was removed
  in favour of the common SSOT

### Changed
- Upload broadcasts now skip hidden files (consistent with the `/files`
  listing): peers no longer receive pushed dotfiles they never asked for
- **Deps**: Updated runtime and development dependencies: uvicorn 0.51.0,
  typer 0.27.0, websockets 16.1.1, aiofiles 25.1.0 (via hsync-common),
  coverage 7.15.2, filelock 3.31.2, platformdirs 4.10.1, ruff 0.15.22.
  Constraint ranges for `typer` and `uvicorn` widened accordingly.
  `fastapi` remains intentionally capped at `<0.137` (see
  trallnag/prometheus-fastapi-instrumentator#370).
- **Docs**: Note in the user documentation (`README.md`, `faq.md`) that the
  desktop GUI (`hsync-desktop`) and Android app (`hsync-android`) are still
  under active development and not yet ready for production use.
- **Docs**: Updated `CONTRIBUTING.md` — the test-structure table now reflects
  the actual test files organised by area (REST API, WebSocket, middleware,
  config, file utils, compression, delta, contract, scripts), and the release
  process documents the tag-driven `release.yml` workflow (push `vX.Y.Z` →
  CI auto-build + push to GHCR + GitHub Release with `install.sh`,
  `compose.yaml`, `SHA256SUMS`) instead of the old manual Docker tag/push
  and release steps.
- **Docs**: `docs/getting-started.md` now shows a one-command quick install
  (`curl -fsSL … | bash`) as the primary path, with the verified multi-step
  download as an alternative. The LAN setup section also shows the one-liner
  with `--bind-address`.
- **Release**: The installer asset is now published as `install.sh` (was
  `install-<version>.sh`) since the version is already in the release URL
  path. The `release.yml` workflow and all documentation URLs have been
  updated accordingly.

## [1.0.0] — 2025-12-xx

### Added
- Initial release: REST API for file upload/download, bucket management
- WebSocket real-time event broadcast
- API Key authentication (Bearer Token)
- SHA-256-based file integrity verification
- Integrated Prometheus metrics
- Docker-ready deployment with multi-arch support (amd64, arm64)
- Rate limiting per endpoint and per IP
- Symbolic link synchronization support
- Per-bucket connection management
- Resumable uploads with offset tracking
- Deletion tombstones (`.hsynctombstones`)
- Clock-synchronization endpoint (`/api/v1/time`)
- Zstandard (zstd) transfer compression
- `docker.sh` management script
- Comprehensive documentation

[1.0.0]: https://github.com/albertobasso/hsync-server/releases/tag/v1.0.0
