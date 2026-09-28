# Benchmarks — hsync-server

One tier today, **L1: pure in-process micro-benchmarks** of the server-specific
hot paths. The shared library primitives (`hsync_common` hashing, compression,
delta, models, path validation, stores) are already covered by
`hsync-common/bench`; this directory measures only what the server adds on top:

| Tier | Status here | Why |
|---|---|---|
| Startup — process spawn cost | **implemented** (`bench_startup.py`) | Cold `import hsync_server.main` + live uvicorn spawn to `/health` readiness (with optional bucket pre-population to isolate the hash preload) |
| L1 — in-process micro-benchmark | **implemented** (this directory) | Measures the auth/rate-limit/version/arbitration/validation wrappers every REST+WS request or upload runs |
| L2/L3 — transport load + soak | **L3 soak implemented** (`soak.py`); L2 wire-latency benchmark deferred | The soak spawns the real server over loopback and measures sustained mixed traffic + RSS; a per-operation L2 latency tier is still a follow-on |
| L4 — profiling diagnostic | n/a (yet) | Added only when a specific anomaly needs a cost breakdown |
| L5 — CPU-saturation sweep | n/a | Multi-process uvicorn topology, not a single Python call — revisit with L2 |

Each script is a **plain script, not a test**: it is never collected by pytest
(`testpaths = ["tests"]`, `python_files = ["test_*.py"]`), never runs in CI, and
asserts only that the measured operation really took the intended branch.

## Isolation

The server's `Settings` singleton is cached (`@lru_cache`), so every benchmark
that touches persisted state (version/arbitration stores open the per-bucket state DB
under `HSYNC_STATE_DIR`; `resolve_within_bucket` resolves under
`HSYNC_BUCKETS`) must redirect those env vars **before** importing the
`hsync_server` modules. `common.isolate_server_state()` does exactly that
(module-level call), so the repository's real `state/`/`buckets/` are never
read or written — mirroring the test suite's `no_tests_touch_real_repo_state`.

## Scripts

| Script | Measures |
|---|---|
| `bench_startup.py` | **Startup** — cold `import hsync_server.main` and live uvicorn spawn → `GET /health` 200, with optional `--files N` bucket pre-population to isolate the `set_sha256_hashes` lifespan preload |
| `soak.py` | **L3 soak (manual, long)** — spawns the real server (temp buckets/state, raised rate limits) and drives mixed upload/overwrite/download-verify/list/delete traffic for `--duration`, sampling the server process RSS/CPU from `/proc`. Verdict: 0 failed ops, final listing + full download re-hash match the expected manifest, RSS delta within `--max-rss-mb`. `--no-compression` stays as a diagnostic to isolate harness/RSS checks from the compression path |
| `bench_auth.py` | `parse_token` (valid / bad-prefix / bad-id / bad-secret), `_cache_key` (SHA-256 of token) — the per-request auth gate (scrypt itself is the rare KDF-cache miss, not measured) |
| `bench_rate_limiter.py` | `get_client_ip` (trusted-proxy + XFF / direct / no-client), `_parse_ws_rate`, `TokenBucket.acquire` (hit / drained) |
| `bench_hashes.py` | `set_sha256_hash` / `get_sha256_hash(hit/miss)` / `del_sha256_hash(hit/miss)` — the per-file hash cache |
| `bench_utils.py` | `resolve_within_bucket` (hit / escape / no-final-deref), `is_valid_bucket` — the per-request confinement gate |
| `bench_file_utils.py` | `is_sentinel_file` (hit/miss), `update_file_timestamp` (normal / far-future clamp / missing) |
| `bench_arbitration.py` | `arbitrate_upload` (accepted / superseded / conflict-won / conflict-lost), `conflicted_copy_name` — the per-upload causal gate |
| `bench_versions.py` | `server_version_info` (recorded / missing), `record_version` — the per-listing/per-upload version store |
| `bench_compression.py` | `new_compressor(level=3)` construction (the per-call compression context), `compress_data(64KiB)` (delta-literal path), `async_compress_stream(256KiB\|1MiB)` (compressed-download helper). Prices the per-call compression isolation |
| `bench_connection_manager.py` | `BucketConnectionManager.try_acquire_slot` (under / global cap / per-IP cap), `release_slot` — the per-WS-join DoS gate |
| `run_all.py` | Chained runner: runs every script above and writes `bench_result.txt` at the repo root; `--json-out-dir DIR` also dumps one `<script>.json` per scenario for A/B comparison |
| `compare.py` | A/B comparator of two `--json-out` runs (single file or directory): flags median regressions over a noise floor, `exit 1` on any regression |
| `bench_result.txt` | **Committed baseline** (default `--count 2000`, this machine): compare revisions with `git diff bench_result.txt` |

## Running

```bash
cd hsync-server

# the whole suite in one go, writing the versioned baseline file
.venv/bin/python bench/run_all.py --count 2000            # -> ../bench_result.txt

# or one script at a time
.venv/bin/python bench/bench_auth.py --count 2000
.venv/bin/python bench/bench_rate_limiter.py --count 2000
.venv/bin/python bench/bench_hashes.py --count 2000
.venv/bin/python bench/bench_utils.py --count 2000
.venv/bin/python bench/bench_file_utils.py --count 2000
.venv/bin/python bench/bench_arbitration.py --count 2000
.venv/bin/python bench/bench_versions.py --count 2000
.venv/bin/python bench/bench_connection_manager.py --count 2000

# machine-readable results (one JSON array per run)
.venv/bin/python bench/bench_auth.py --count 2000 --json-out /tmp/auth.json

# startup benchmark (standalone: spawns processes, not in run_all.py)
.venv/bin/python bench/bench_startup.py --count 10
.venv/bin/python bench/bench_startup.py --count 10 --files 5000 --json-out /tmp/startup.json

# L3 soak (standalone: spawns a real server, long-running)
.venv/bin/python bench/soak.py --duration 600
.venv/bin/python bench/soak.py --duration 600 --no-compression

# A/B comparison over two full runs
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/before
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/after
.venv/bin/python bench/compare.py /tmp/before /tmp/after --threshold 1.10
```

`--count` is the number of timed samples for the cheap operations. The
persist-per-op paths (`record_version`, `arbitrate_upload` store reads) run at a
lower effective sample count where a default run would otherwise be dominated by
SQLite/file I/O; the reported `n` column shows the effective count.

## Methodology

* **Warmup outside the samples**: warmup iterations run before timing and are
  scaled to `min(200, count // 10)`.
* **Setup outside the timed region**: fixtures (temp bucket/state dir, seeded
  stores, pinned config getters, stub requests) are prepared once per scenario.
* **Branch assertions inside the measured op**: every sample asserts the
  returned value/outcome/verdict for that branch — a silent fallback or a
  short-circuited store cannot be reported as a fast success.
* **One scenario, one fixture lifecycle**: state-dependent scenarios run in a
  per-process temp dir (`HSYNC_STATE_DIR`/`HSYNC_BUCKETS`) and clear the module
  store/cache (e.g. `versions.reset_cache`, `FILE_HASHES.clear`) so no scenario
  is faster because a previous one warmed shared state.
* **Deterministic ids**: counter-based `make_cid`/`make_hex`, no `random` seed
  juggling.
* **Full latency stats**: every result reports `mean`/`p50`/`p99` plus
  `stdev`/`min`/`max`, so a later comparison can tell a genuine slowdown from a
  noisy tail.
* **Comparisons are relative**: numbers are valid for this machine and a warm OS
  page cache; they compare implementations, not production SLAs.

## Shared helpers

`bench/common.py` is stdlib-only and **never imports `hsync_server`**: the
harness must stay usable if the server internals are rewritten.

* measurement core — `BenchResult`, `measure`, `measure_async`, `percentile`,
  `scaled_warmup`, `print_table`, `add_json_out`, `maybe_write_json`;
* deterministic ids — `make_cid`, `make_cids`, `make_hex`, `ALPHABET`;
* state isolation — `isolate_server_state` (redirects `HSYNC_STATE_DIR` /
  `HSYNC_BUCKETS` to a temp dir before importing any `hsync_server` module).

`bench/compare.py` is equally stdlib-only and never imports `hsync_server`; its
pure `compare()` decision function is pinned by `tests/test_bench_compare.py`.
