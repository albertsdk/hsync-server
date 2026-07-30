# HSYNC Server — Testing Guide

This guide covers the test suite for the HSYNC Server: structure, how to run
tests, how to write new tests, and cross-project testing with `hsync-common`.

> For development setup, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Table of Contents

- [Running tests](#running-tests)
- [Test structure](#test-structure)
- [Fixtures](#fixtures)
- [Writing new tests](#writing-new-tests)
- [Contract testing (OpenAPI)](#contract-testing-openapi)
- [Performance testing](#performance-testing)
- [Cross-project testing](#cross-project-testing)

---

## Running tests

```bash
# Install dev dependencies (already set up via committed virtualenv)
# If needed: poetry install

# Run the full suite
.venv/bin/python -m pytest

# Run a specific file
.venv/bin/python -m pytest tests/test_api_files_crud.py

# Run with verbose output
.venv/bin/python -m pytest -v

# Run with coverage
.venv/bin/python -m pytest --cov=hsync_server --cov-report=term-missing

# Run only tests matching a pattern
.venv/bin/python -m pytest -k "upload"

# Run only integration tests
.venv/bin/python -m pytest -m integration

# Run unit tests only (exclude integration)
.venv/bin/python -m pytest -m "not integration"

# Stop on first failure
.venv/bin/python -m pytest -x
```

## Test structure

```
tests/
├── __init__.py
├── conftest.py                         # Shared fixtures and test configuration
├── test_api_files_advanced.py          # File operations: advanced scenarios
├── test_api_files_crud.py              # File CRUD: upload, download, list, delete
├── test_api_files_validation.py        # File operations: input validation
├── test_api_integration.py             # End-to-end integration tests
├── test_api_utils.py                   # API utility helpers
├── test_bb_auth_protocol_limits.py     # Black-box: auth, protocol version and rate-limit use cases
├── test_bb_bucket_lifecycle.py         # Black-box: bucket create/read/list/delete lifecycle, isolation, presence
├── test_bb_client_interop.py           # Black-box: real hsync-client CLI driven from the server repo (TLS interop)
├── test_bb_delta_transfer.py           # Black-box: delta transfer use case (signatures, tokens, apply)
├── test_bb_download_compression.py     # Black-box: download-side zstd compression for large files
├── test_bb_file_download_delete.py     # Black-box: file download and delete use cases
├── test_bb_file_upload.py              # Black-box: file upload use case (resume, compression, overwrite)
├── test_bb_members_presence.py         # Black-box: bucket membership (grant/list/revoke) and presence counters
├── test_bb_tombstones_status.py        # Black-box: tombstone listing, merge, and resumable upload-status
├── test_bb_websocket_relay.py          # Black-box: WebSocket event-relay (handshake, fan-out, echo suppression)
├── test_compression.py                 # Zstandard compression unit tests
├── test_config.py                      # Configuration loading and validation
├── test_connection_manager.py          # WebSocket connection pool management
├── test_coverage_gaps.py               # Coverage gap fillers
├── test_delta_endpoints.py             # Delta transfer REST endpoints
├── test_docker_script.py               # docker.sh script behavior
├── test_error_handlers.py              # Global exception handler tests
├── test_event_handlers.py              # WebSocket event business logic
├── test_file_utils.py                  # File system operations and safety checks
├── test_hashes.py                      # SHA-256 computation and cache
├── test_health.py                      # Health check endpoint tests
├── test_install_script.py              # install.sh script tests
├── test_logger.py                      # Logging configuration
├── test_main.py                        # Application lifecycle and startup
├── test_middleware.py                  # Middleware chain behavior
├── test_models.py                      # Data model validation
├── test_models_sync_action.py          # SyncAction model tests
├── test_openapi_contract.py            # OpenAPI schema contract tests
├── test_rate_limiter.py                # Per-endpoint rate limiting
├── test_routers.py                     # Router wiring and auth dependencies
├── test_security.py                    # Authentication and security tests
├── test_ssl_utils.py                   # Certificate generation and SSL context
├── test_token_bucket.py                # Token bucket algorithm unit tests
└── test_websocket.py                   # WebSocket endpoint and event flow
```

## Fixtures

`conftest.py` provides shared fixtures used across the test suite:

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `fast_scrypt` (autouse) | function | Low-cost scrypt params, isolated temp `state/` and `buckets/` dirs (monkeypatched per module), cache resets, admin bootstrap |
| `no_tests_touch_real_repo_state` (autouse) | function | Fails any test that mutates the repository's real `state/syncstate.db` or creates/removes dirs under the real `buckets/` |
| `deterministic_cli_output` (autouse) | function | Forced plain, uncolored CLI output (CI-safe assertions) |
| `_prevent_os_environ_leakage` (autouse) | function | Removes env vars added by a test on teardown |
| `_scrub_hsync_env` (autouse) | function | Removes every `HSYNC_*` inherited from the developer's shell (except `HSYNC_ADMIN_KEY`) so `Settings()` defaults are hermetic |
| `client` / `client_no_auth` | function | Async HTTPX clients against a fresh FastAPI test app, with/without admin auth |
| `test_app` | function | Assembled router/handler stack on isolated state (no uvicorn) |
| `live_server` / `start_live_server` / `bb_http` / `bb_bucket` | module/function | Black-box harness: real server subprocess on an ephemeral port with scrubbed `HSYNC_*` env and temp dirs |

### Using fixtures

All file paths inside a test go through `get_buckets_path()` / `get_state_dir()`,
which the `fast_scrypt` fixture has already pointed at the test's temp dirs:

```python
import os
from pathlib import Path

import pytest

from hsync_server.config import get_buckets_path


@pytest.mark.asyncio
async def test_upload_file(client):
    """Upload a file and verify it's stored correctly."""
    os.makedirs(Path(get_buckets_path()) / "test-bucket", exist_ok=True)
    response = await client.post(
        "/api/v1/buckets/test-bucket/files",
        files={"file": ("hello.txt", b"Hello, World!", "text/plain")},
    )
    assert response.status_code == 201
    stored = Path(get_buckets_path()) / "test-bucket" / "hello.txt"
    assert stored.read_text() == "Hello, World!"
```

## Test isolation — never touch the repository's real state

The server's settings default to the repository layout (absolute, resolved
`<repo>/buckets` and `<repo>/state`, `config.py:_HSYNC_PATH`), so a test that
forgets to isolate its state would silently pollute the developer's real
`syncstate.db` and `buckets/` with test rows and directories (the historical
cause of "ghost" test buckets such as `bb-*` shown by `admin bucket list`).
The suite enforces isolation at **three independent levels**:

1. **`fast_scrypt` (autouse)** — every in-process test runs against temporary
   `state/` and `buckets/` dirs: `get_state_dir` and `get_buckets_path` are
   monkeypatched in every module that binds them at import time (the list in
   `conftest.py` is the single place to extend when a new module imports those
   helpers), followed by cache resets and admin bootstrap.
2. **Black-box harness** — the live server runs as a **subprocess** whose
   environment is scrubbed of every `HSYNC_*` variable and re-pointed to
   `tempfile.mkdtemp` directories (`conftest.py:_start_live_server`).
3. **`no_tests_touch_real_repo_state` (autouse safety net)** — before and after
   each test it takes a read-only logical snapshot of the repository's real
   `state/syncstate.db` (tables `clients`, `bucket_members`, `buckets`,
   `folders`, `versions`, `tombstones`; opened with `mode=ro`, so the snapshot
   itself can never create `-wal`/`-shm` side files) and of the real `buckets/`
   directory set. Any difference fails the **offending test** with an
   explanatory message — leftovers become a red test instead of silent
   pollution.

Configuration is isolated separately by **`_scrub_hsync_env` (autouse)**: every
in-process test runs with the developer's `HSYNC_*` variables removed (only
`HSYNC_ADMIN_KEY` survives — the conftest sets it at import time for the
bootstrap admin), so `Settings()` default-value assertions are hermetic and
independent of the execution environment; `monkeypatch.delenv` restores them on
teardown. This mirrors the same fixture in `hsync-client`'s conftest. Note it
covers per-test reads only: the rate limiter captures `HSYNC_RATE_LIMIT_*` at
module import time (`rate_limiter.py:55`), which a per-test scrub cannot undo.

> If a run ever leaves test rows behind anyway (for example from an older run),
> clean them up with
> `hsync-server admin bucket delete --bucket <name> --db-only`, which removes
> only the DB rows of a ghost bucket whose directory no longer exists.

## Writing new tests

### File operation tests

For tests involving file uploads, downloads, or deletions:

1. Use `test_client` for HTTP requests
2. Use `temp_bucket` for isolated filesystem state
3. Include `auth_headers` to satisfy the API key requirement
4. Verify both the HTTP response and the filesystem state

### WebSocket tests

WebSocket tests use FastAPI's `TestClient.websocket_connect()`:

```python
def test_websocket_event_broadcast(test_client, auth_headers):
    with test_client.websocket_connect("/api/v1/buckets/test-bucket/events/test-cid") as ws:
        # Send auth message first
        ws.send_json({"type": "auth", "api_key": "test-key"})
        response = ws.receive_json()
        assert response["type"] == "auth_ok"
```

### Error case tests

For every endpoint, test at least these error scenarios:

- Missing authentication (401)
- Invalid API key (401)
- Rate limit exceeded (429)
- Invalid input (422)
- Resource not found (404)
- Path traversal attempts (400 or 422)

### Rate limiter tests

Rate limiter tests need to simulate multiple rapid requests:

```python
def test_rate_limit_enforced(test_client):
    for _ in range(501):  # Default is 500/minute
        test_client.get("/health")
    response = test_client.get("/health")
    assert response.status_code == 429
```

## Contract testing (OpenAPI)

`test_openapi_contract.py` validates the generated OpenAPI schema against
expectations. It ensures:

- All endpoints are documented with correct HTTP methods
- Request and response schemas match the implementation
- Authentication requirements are correctly specified
- New endpoints are not accidentally exposed without auth

When adding a new endpoint:

1. Add the endpoint to the router
2. Run `poetry run python scripts/export_openapi.py` to regenerate the schema
3. Update contract test expectations
4. Update `docs/api.md` with the new endpoint documentation

## Performance testing

Performance benchmarks are not currently configured. If you need to measure
performance-sensitive changes (hashing, compression, file I/O), consider using
`pytest-benchmark` or dedicated profiling tools. The test suite focuses on
correctness and contract validation rather than microbenchmarks.

## Black‑box tests (`test_bb_*.py`)

The `test_bb_*.py` files are **behavioural, use‑case‑driven** tests that
drive the **real** server over loopback HTTP and WebSocket. No internal
logic is mocked — only the filesystem (temp buckets directory) and the
API key are test fixtures.

**These tests are disabled by default** (opt‑in via ``HSYNC_BB_SERVER=1``),
because they spin up a live server subprocess and require more resources
than unit tests.

| File | What it covers |
|------|---------------|
| `test_bb_auth_protocol_limits.py` | Authentication, protocol‑version mismatch, rate‑limit enforcement, auth negatives on protected routes. |
| `test_bb_bucket_lifecycle.py` | Bucket create / read / list / delete lifecycle, cross‑bucket isolation, presence. |
| `test_bb_client_interop.py` | Real hsync‑client CLI driven from the server repo over TLS (capabilities, upload, two‑client convergence). |
| `test_bb_delta_transfer.py` | Delta transfer use case (signatures, tokens, apply, negatives). |
| `test_bb_download_compression.py` | Download-side zstd compression for large files (`Content-Encoding: zstd`). |
| `test_bb_file_download_delete.py` | File download and delete use cases. |
| `test_bb_file_upload.py` | File upload use case (resume, compression, overwrite, validation). |
| `test_bb_members_presence.py` | Bucket membership (grant/list/revoke) and presence counters with a connected member. |
| `test_bb_tombstones_status.py` | Tombstone listing, causal merge, and resumable upload‑status. |
| `test_bb_websocket_relay.py` | WebSocket event‑relay (handshake, fan‑out, echo suppression, rejection). |

To opt in:
```bash
HSYNC_BB_SERVER=1 .venv/bin/python -m pytest tests/test_bb_*.py
```

## Cross-project testing

The server depends on `hsync-common` models. When changing models in
`hsync-common`, always run both test suites:

```bash
# From hsync-common
cd ../hsync-common && .venv/bin/python -m pytest

# From hsync-server
cd ../hsync-server && .venv/bin/python -m pytest
```

Model changes that break the server are caught by the server's model tests
(`test_models.py`, `test_models_sync_action.py`) and contract tests.

## See also

- [CONTRIBUTING.md](../../CONTRIBUTING.md) — development setup and workflow.
- [Developer Guide](developer.md) — internal implementation details.
- [API Reference](../api.md) — endpoint documentation for writing API tests.
- [Architecture](architecture.md) — system overview and request lifecycle.
