# HSYNC Server — Benchmarks (L1 + startup)

Internal micro-benchmark suite for the server-specific hot paths. Developer
tooling: plain scripts under `bench/`, never collected by pytest, never run in
CI, no user-facing behaviour.

## Why one tier (today)

`hsync-server` is a FastAPI process, but its hot layers are the thin wrappers
it adds on top of `hsync_common` (token parsing, trusted-proxy IP resolution,
hash cache freshness, bucket confinement, upload arbitration, version-store
bindings, connection slot accounting). The shared primitives are already
covered by `hsync-common/bench`; here we measure only the server's own wrapper.

L2/L3 (spawn a live uvicorn server over loopback, wire-only) is deferred until
this baseline is reviewed. L4/L5 are added only when a concrete anomaly needs a
cost breakdown / a CPU-saturation sweep.

## Isolation (critical)

The server `Settings` singleton is cached (`config.py:339` `@lru_cache`), and its
defaults point at the repo's own `state/`/`buckets/`. Every benchmark that
touches persisted state sets `HSYNC_STATE_DIR`/`HSYNC_BUCKETS` to a fresh temp
dir **before** importing any `hsync_server` module — `common.isolate_server_state()`
(`bench/common.py`) — so the repository's real `state/`/`buckets/` are never read
or written (mirroring the test suite's `no_tests_touch_real_repo_state`).

## Files

| Path | Role |
| --- | --- |
| `bench/README.md` | Tier table, per-script scope, run commands, isolation + methodology |
| `bench/common.py` | Stdlib-only measurement core (mean/p50/p99/stdev/min/max) + deterministic ids + `isolate_server_state`; never imports `hsync_server` |
| `bench/bench_auth.py` | `auth_store.parse_token` / `auth._cache_key` scenarios |
| `bench/bench_rate_limiter.py` | `rate_limiter.get_client_ip` / `_parse_ws_rate` / `TokenBucket.acquire` scenarios |
| `bench/bench_hashes.py` | `hashes.set/get/del_sha256_hash` cache scenarios |
| `bench/bench_utils.py` | `api/utils.resolve_within_bucket` / `is_valid_bucket` scenarios |
| `bench/bench_file_utils.py` | `api/file_utils.is_sentinel_file` / `update_file_timestamp` scenarios |
| `bench/bench_arbitration.py` | `arbitration.arbitrate_upload` / `conflicted_copy_name` scenarios |
| `bench/bench_versions.py` | `versions.server_version_info` / `record_version` scenarios |
| `bench/bench_connection_manager.py` | `api/connection_manager` slot-accounting scenarios |
| `bench/bench_startup.py` | **Startup** — cold `import hsync_server.main` + live uvicorn spawn → `GET /health` 200 (`--files N` isolates the hash preload) |
| `bench/run_all.py` | Chained runner → `bench_result.txt` (committed baseline); `--json-out-dir` |
| `bench/compare.py` | A/B comparator over two `--json-out` runs; flags median regressions above a noise floor, `exit 1` on regression |

## Scenario → code anchors

| Scenario | Public API measured |
| --- | --- |
| `parse_token(valid\|bad_prefix\|bad_id\|bad_secret)` | `auth_store.parse_token` (`hsync_server/auth_store.py:191`) — wire token gate, one per REST/WS auth |
| `auth_cache_key(sha256)` | `auth._cache_key` (`hsync_server/auth.py:74`) — SHA-256(token) KDF cache key |
| `get_client_ip(trusted+xff\|direct)` | `rate_limiter.get_client_ip` (`hsync_server/rate_limiter.py:74`) — trusted-proxy gate |
| `parse_ws_rate(10/second)` | `rate_limiter._parse_ws_rate` (`:170`) |
| `token_bucket.acquire(hit\|drained)` | `rate_limiter.TokenBucket.acquire` (`:220`) — per-WS-message budget |
| `set_sha256_hash` / `get_sha256_hash(hit\|miss)` / `del_sha256_hash(hit\|miss)` | `hashes` (`hsync_server/hashes.py:29,:52,:83`) — per-file cache + freshness check |
| `resolve_within_bucket(hit\|escape\|no-deref)` | `api.utils.resolve_within_bucket` (`hsync_server/api/utils.py:50`) — per-request confinement |
| `is_valid_bucket` | `api.utils.is_valid_bucket` (`:34`) |
| `is_sentinel_file(hit\|miss)` | `api.file_utils.is_sentinel_file` (`hsync_server/api/file_utils.py:27`) |
| `update_file_timestamp(normal\|clamp\|missing)` | `api.file_utils.update_file_timestamp` (`:110`) — mtime restore + far-future clamp |
| `arbitrate_upload(first\|dominant\|superseded\|conflict_won\|conflict_lost)` | `arbitration.arbitrate_upload` (`hsync_server/arbitration.py:74`) — per-upload causal gate |
| `conflicted_copy_name` | `arbitration.conflicted_copy_name` (`:193`) |
| `server_version_info(recorded\|missing)` | `versions.server_version_info` (`hsync_server/versions.py:145`) — per-listing version |
| `record_version(persist)` | `versions.record_version` (`:74`) — per-upload record + SQLite save |
| `try_acquire_slot(under\|global_cap\|per_ip_cap)` / `release_slot` | `api.connection_manager.BucketConnectionManager` (`hsync_server/api/connection_manager.py:151,:163`) — per-WS-join DoS gate |

## Initial findings (baseline `bench_result.txt`, count=2000, this machine)

* **The per-request gates are sub-microsecond** — `parse_token(valid)` ~0.0006 ms
  (after `be50fbf`, was ~0.0011 ms: one precompiled fullmatch
  `auth_store.py:191` instead of a 21-iteration generator, **~1.8x**;
  `parse_token(bad_secret)` ~0.0009→0.0005 ms),
  `auth_cache_key` ~0.0005 ms, `get_client_ip` ~0.0002 ms, `is_valid_bucket`
  ~0.0003 ms, the slot accounts ~0.0002 ms. None of them is a request-lifecycle
  bottleneck; slowapi/scrypt dominate the real thing. `server_version_info`
  hoists the `VersionInfo` import (`versions.py`): `recorded` ~0.0029→0.0024 ms,
  `missing` ~0.0024→0.0019 ms; `arbitrate_upload` ~0.0048→~0.0037 ms on every
  branch and `record_version(persist)` ~0.096→0.020 ms (**~4.8x**) ride the
  shared `hsync-common` store opts. Committed baseline `bench_result.txt`
  2026-09-23; A/B re-run isolated: **0 regressions** over 37 ops (threshold
  1.10, floor 0.001 ms).
* **Bucket confinement resolves on disk** — `resolve_within_bucket(hit)` ~0.037 ms
  is the per-request floor: `Path(...).resolve()` (a realpath syscall) dominates
  the hit path, while the escaping `../` reject is ~0.0006 ms (lexical guard
  fires before any syscall). Same structure as `hsync-common`'s `resolve_within`.
* **The upload gate is branch-insensitive** — `arbitrate_upload` ~0.0046 ms (~3.4x
  cheaper than the first baseline's ~0.0156 ms): the dominant cost was the
  `.hsyncnodeid` sidecar re-read (``get_server_cid`` re-opened and re-validated
  the file on every upload); memoizing it in-process (``versions.py`` →
  ``get_server_cid``, invalidated by ``reset_cache``) removed the per-upload file
  read. What remains is the version-store read + wire-version parse; the verdict
  (accept/supersede/conflict) adds nothing measurable. `conflicted_copy_name`
  ~0.0029 ms.
* **Per-listing version is cheap, per-upload persist is not** —
  `server_version_info(recorded)` ~0.0030 ms (relpath + store read) vs
  `record_version(persist)` ~0.097 ms (a SQLite WAL write) — the upload path's
  persistence cost is ~30x the read path.
* **Hash cache freshness check** — `get_sha256_hash(hit)` ~0.0018 ms (SHA-256 of
  the path + one `os.stat` + compare); the miss is ~0.0005 ms (no stat).

## Startup findings (`bench/bench_startup.py`, this machine)

* **Module import dominates** — cold `import hsync_server.main` ≈ 750–880 ms
  (FastAPI + `prometheus_fastapi_instrumentator` + `slowapi` + middleware +
  `hsync_common`). This is the irreducible floor without dropping those
  dependencies.
* **Lifespan is small on top** — spawn → `GET /health` 200 ≈ 1.1 s with empty
  buckets; the bootstrap + empty preload + synchronous VACUUM add ~200–300 ms.
* **Hash preload scales with data** — with `--files 5000` readiness rises to
  ≈ 1.3 s, i.e. `set_sha256_hashes` adds ~200 ms for 5000 files (linear).
* **Decision: preload is NOT removed.** The preload is a pure freshness-checked
  cache, but several request paths read it **directly** without the
  recompute-on-miss fallback: `list.py:96` (listing `sha256sum` + HMAC),
  `download.py:109` (download header), `upload.py:272/:523` (dedup/disk hash),
  `apply.py:302` (delta base hash). Dropping the eager preload would return
  empty/`None` hashes on those paths until recomputed — a correctness change,
  not a timing one. Converting all of them to `get_or_compute_sha256` is a broad
  async refactor across the file API. Per the robustness-over-speed rule, the
  preload stays and the startup cost is reported as-is.

## Running

```bash
cd hsync-server
.venv/bin/python bench/run_all.py --count 2000            # -> bench_result.txt
.venv/bin/python bench/bench_auth.py --count 2000 [--json-out FILE]
# ... (one script per module; see bench/README.md)

# A/B regression check between two full-suite JSON dirs
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/before
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/after
.venv/bin/python bench/compare.py /tmp/before /tmp/after --threshold 1.10

# startup benchmark (standalone, not in run_all.py)
.venv/bin/python bench/bench_startup.py --count 10
.venv/bin/python bench/bench_startup.py --count 10 --files 5000 --json-out /tmp/startup.json
```

Lint: `cd hsync-server && .venv/bin/python -m ruff check bench` — the directory
is covered by the project ruff config, so the Quality Gate applies to `bench/`
too.
