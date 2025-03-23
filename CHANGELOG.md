# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
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
  the declaration
- **WebSocket event hardening**: every event `src`/`dst` path is validated
  (`validate_remote_relpath`) before dispatch or relay, dropping traversal,
  absolute and backslash payloads; connection CIDs are format-validated
- **Permissions hardening**: setuid/setgid/sticky bits are stripped from
  client-supplied permission strings before being applied
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
