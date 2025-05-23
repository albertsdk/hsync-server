# Contributing to hsync-server

Thanks for your interest in contributing! This guide covers the development
workflow for the HSYNC server (FastAPI REST + WebSocket).

## Prerequisites

- Python `>=3.12,<3.15` (lint/CI target: **3.13**)
- [Poetry](https://python-poetry.org/) for dependency management
- A checkout of [`hsync-common`](../hsync-common) as a **sibling directory**
  (declared as a Poetry path dependency)

## Setup

```bash
poetry install
```

## Running

```bash
HSYNC_API_KEY=dev-key poetry run python -m hsync_server
# or use the provided dev.env template
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and
formatting (line length 100, target `py313`), enforced via pre-commit hooks.

```bash
poetry run ruff check --fix .
poetry run ruff format .
```

## Pre-commit Workflow

Pre-commit hooks run ruff (and an end-of-file fixer) on commit. When a hook
auto-fixes files, the commit aborts because files changed — **this is expected,
not a failure**. Re-stage and commit again:

```bash
poetry run ruff check --fix .
poetry run ruff format .
git add -A
git commit -m "type(scope): message"
# if hooks modified files and the commit aborted, re-stage and retry:
git add -A
git commit -m "type(scope): message"
```

## Tests

```bash
# Run the full test suite
poetry run pytest

# Run a specific test file
poetry run pytest tests/test_api_files.py

# Run with coverage report
poetry run pytest --cov=hsync_server --cov-report=term-missing

# Run only integration tests
poetry run pytest -m integration

# Run only unit tests (exclude integration)
poetry run pytest -m "not integration"
```

If the project configures mypy (see `pyproject.toml`), also run
`poetry run mypy .`.

### Test structure

All tests live at the project root under `tests/`. The suite is organised by
area; each file name starts with `test_` and groups related functionality.

| Area | Key files | What they cover |
|------|-----------|-----------------|
| **REST API** | `test_api_files.py`, `test_api_integration.py`, `test_api_utils.py`, `test_routers.py`, `test_health.py` | File endpoints, integration flows, utilities, router wiring, health |
| **WebSocket** | `test_websocket.py`, `test_connection_manager.py`, `test_event_handlers.py` | WS connection lifecycle, connection manager, event handlers |
| **Middleware / security** | `test_middleware.py`, `test_security.py`, `test_rate_limiter.py`, `test_token_bucket.py`, `test_error_handlers.py` | Middleware chain, auth, rate limiting, token bucket, error handlers |
| **Config / main** | `test_config.py`, `test_main.py`, `test_logger.py` | Config loading, app startup, logging |
| **File / hash utils** | `test_file_utils.py`, `test_hashes.py`, `test_ssl_utils.py` | File operations, SHA-256 cache, SSL utilities |
| **Compression** | `test_compression.py` | Zstandard compression/decompression |
| **Delta transfer** | `test_delta_endpoints.py` | Delta transfer REST endpoints |
| **Contract / models** | `test_openapi_contract.py`, `test_models.py`, `test_models_sync_action.py` | OpenAPI schema drift, data model validation |
| **Scripts** | `test_docker_script.py`, `test_install_script.py` | `docker.sh` and `install.sh` script tests |
| **Other** | `test_coverage_gaps.py` | Coverage gap fillers |
| **Shared** | `tests/conftest.py` | Shared fixtures: test client, temp buckets, auth headers |

### Writing new tests

1. Use the fixtures from `conftest.py` where possible
2. Tests that require a running server use `TestClient` (FastAPI's sync test client)
3. WebSocket tests use `TestClient.websocket_connect()`
4. Add contract tests for any new API endpoints in `test_openapi_contract.py`
5. Mark slow tests with `@pytest.mark.integration`

### Cross-project testing

The server depends on `hsync-common` models. When you change a model in
`hsync-common`, run the server's test suite to verify nothing breaks:

```bash
cd ../hsync-common && poetry run pytest && cd ../hsync-server && poetry run pytest
```

## Release Process

Releases are **tag-driven**: pushing a `vX.Y.Z` tag triggers the
`release.yml` GitHub Actions workflow, which builds and pushes the multi-arch
(amd64 + arm64) Docker image to GHCR, then creates a GitHub Release with
`install.sh`, `compose.yaml`, and `SHA256SUMS` as assets. The Dockerfile
depends on `../hsync-common` at build time, so the workflow checks it out
via a PAT.

### Version bump

```bash
./bump-version.sh 1.0.1
```

This updates the version in `pyproject.toml` and `hsync_server/__init__.py`.

### Build Docker image (local / manual)

The release workflow builds and pushes automatically. To build locally for
testing:

```bash
# Build multi-arch image (amd64 + arm64)
./docker.sh build --multi-arch
```

### Create a release

1. Ensure the working tree is clean and on `main`.
2. Bump the version (see [Version bump](#version-bump)) and commit the change.
3. Tag the commit and push the tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. The `release.yml` workflow runs automatically: it builds and pushes the
   Docker image to `ghcr.io/albertobasso/hsync-server:vX.Y.Z` and
   `:latest`, then creates the GitHub Release with auto-generated release
   notes.
5. Verify the release on the
   [Releases page](https://github.com/albertobasso/hsync-server/releases).

### Post-release checklist

- [ ] Docker image is available on GHCR (`:vX.Y.Z` and `:latest`)
- [ ] Release assets (`install.sh`, `compose.yaml`, `SHA256SUMS`) are uploaded
- [ ] Release notes are published on GitHub
- [ ] `docs/api.md` is up to date with any endpoint changes
- [ ] Environment variable table in `docs/README.md` is updated
- [ ] Notify downstream projects (hsync-client, hsync-desktop) if protocol changed

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, …).

## Pull Requests

- Keep changes focused and covered by tests.
- Keep the API reference (`docs/api.md`) and rate-limit table
  (`README.md`) in sync with the code when you change endpoints or limits.
- Ensure `ruff` and `pytest` pass locally before opening a PR.
