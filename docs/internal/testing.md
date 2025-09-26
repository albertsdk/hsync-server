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
.venv/bin/python -m pytest tests/test_api_files.py

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
├── test_bb_bucket_lifecycle.py         # Black-box: bucket create/read/list/delete lifecycle
├── test_bb_delta_transfer.py           # Black-box: delta transfer use case (signatures, tokens, apply)
├── test_bb_file_download_delete.py     # Black-box: file download and delete use cases
├── test_bb_file_upload.py              # Black-box: file upload use case (resume, compression, overwrite)
├── test_bb_tombstones_status.py        # Black-box: tombstone listing and resumable upload-status
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
| `test_client` | function | FastAPI `TestClient` instance for sync HTTP testing |
| `temp_bucket` | function | Temporary bucket directory with test files |
| `auth_headers` | function | Pre-built `Authorization: Bearer` headers |
| `api_key` | session | Random API key valid for the test session |
| `mock_config` | function | Patched configuration for isolated tests |

### Using fixtures

```python
def test_upload_file(test_client, temp_bucket, auth_headers):
    """Upload a file and verify it's stored correctly."""
    response = test_client.post(
        "/api/v1/buckets/test-bucket/files",
        headers=auth_headers,
        files={"file": ("hello.txt", b"Hello, World!", "text/plain")},
    )
    assert response.status_code == 201
    assert (temp_bucket / "hello.txt").read_text() == "Hello, World!"
```

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
    with test_client.websocket_connect(
        "/api/v1/buckets/test-bucket/events/test-cid"
    ) as ws:
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
| `test_bb_auth_protocol_limits.py` | Authentication, protocol‑version mismatch, rate‑limit enforcement. |
| `test_bb_bucket_lifecycle.py` | Bucket create / read / list / delete lifecycle. |
| `test_bb_delta_transfer.py` | Delta transfer use case (signatures, tokens, apply). |
| `test_bb_file_download_delete.py` | File download and delete use cases. |
| `test_bb_file_upload.py` | File upload use case (resume, compression, overwrite, validation). |
| `test_bb_tombstones_status.py` | Tombstone listing and resumable upload‑status. |
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
