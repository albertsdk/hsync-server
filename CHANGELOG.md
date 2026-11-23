# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `bench/bench_api.py` measures per-endpoint REST latency plus the
  listing/download concurrency curve with the node's CPU% (the single-process
  ceiling); `bench/server_process.py` is the shared launcher it and `soak.py`
  now use.
- `HSYNC_EXPOSE_VERSION_HEADERS` (default `false`) opts in to the
  `X-Server-Version`/`X-Protocol-Version` response headers.

### Changed

- Upload arbitration orders concurrent uploads by the advertised corrected mtimes
  (a fixed one-second guard; content identity within it). The receiver's own clock
  trust never gates it: a host that cannot vouch for its clock already advertises
  `corrected_mtime = 0`, so the decision is a pure function of the replicated
  metadata and the server (which never measures a reference) still ranks the
  uploads chronologically (ADR-0054).
- `POST /delta/apply` requires `payload_size` in the metadata part (the literal
  payload's wire bytes): a missing or non-numeric value is a `400`, mirroring the
  P2P master. The field is the progress total the peer reports on.
- The file listing resolves each entry's bucket-relative path once and fetches
  the version store and its lock once per pass, instead of a registry lookup
  (with an `os.getpid` fork check) per entry.
- Responses no longer advertise the server build by default: the version headers
  are opt-in, uvicorn's `Server` header is suppressed, a `426` echoes only the
  caller's own protocol value, and 404/405 answer a generic `{"error": …}`
  envelope instead of the framework's `{"detail": …}` shape.
- A validation error (`422`) keeps its field-level `validation_errors` in the log
  only, so the response no longer maps the request schema to the caller.
- Enabling `HSYNC_ENABLE_DOCS` on a non-loopback bind logs a warning: the shipped
  Swagger UI and OpenAPI schema describe the whole API to whoever reaches them.

### Fixed

- `DELETE …/files` prunes the directory the deletion emptied (and its emptied
  ancestors) instead of keeping it as an intentional empty folder, so a folder a
  peer deleted no longer comes back while its files stay deleted.
- The file listing no longer re-hashes the bucket at startup or past 10k files:
  it reads the preloaded SHA-256 cache through `register_sha256_cache_provider`,
  and the streamed JSON is batched, so a 1,073-file listing drops from ~471 ms
  to ~39 ms p50 (and the first listing after the preload from ~158 ms to ~5 ms
  on a 200x1 MiB bucket).
- An upload whose filename exceeds 255 bytes answers `400`/`HSYNC-1007` instead
  of failing with a `500`: the portable-name policy rejects it before the
  filesystem write (`ENAMETOOLONG`).
- A graceful shutdown releases the process-wide state DB handles
  (`close_shared_state`), so the final WAL checkpoint runs and no connection is
  left to the garbage collector; the admin subcommands, which never enter the
  lifespan, close through `atexit`.

## [1.1.0] - 2026-11-11

### Changed

- Dependency refresh: `typer` 0.29.

### Fixed

- A resumable upload whose preflight offset no longer matches the server's
  `.part` (a duplicate writer appended bytes, or the partial was already
  renamed) answers `202` with the true offset and keeps the partial — the peer
  re-probes and resumes instead of failing permanently.
- Concurrent uploads to the same destination are serialized, so a duplicate
  trigger (watcher + full sync) can no longer corrupt the `.part` with two
  concurrent writers.
