# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The file listing no longer re-hashes the bucket at startup or past 10k files:
  it reads the preloaded SHA-256 cache through `register_sha256_cache_provider`,
  and the streamed JSON is batched, so a 1,073-file listing drops from ~471 ms
  to ~39 ms p50 (and the first listing after the preload from ~158 ms to ~5 ms
  on a 200x1 MiB bucket).

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
