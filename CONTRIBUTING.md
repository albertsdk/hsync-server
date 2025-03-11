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

| Directory | Purpose |
|-----------|---------|
| `tests/` | All test files live at the project root |
| `tests/conftest.py` | Shared fixtures: test client, temp buckets, auth headers |
| `tests/test_api_*.py` | REST API endpoint tests (files, buckets, health) |
| `tests/test_websocket.py` | WebSocket connection and event tests |
| `tests/test_compression.py` | Zstandard compression/decompression unit tests |
| `tests/test_hashes.py` | SHA-256 hashing and cache tests |
| `tests/test_config.py` | Configuration loading and validation |
| `tests/test_middleware.py` | Middleware chain behavior |
| `tests/test_rate_limiter.py` | Rate limiting logic |
| `tests/test_openapi_contract.py` | OpenAPI schema contract tests |
| `tests/test_models*.py` | Data model validation (shared via hsync-common) |

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

### Version bump

```bash
./bump-version.sh 1.0.1
```

This updates the version in `pyproject.toml` and `hsync_server/__init__.py`.

### Build and publish Docker image

```bash
# Build multi-arch image (amd64 + arm64)
./docker.sh build --multi-arch

# Tag and push to GHCR
docker tag hsync-server:latest ghcr.io/albertobasso/hsync-server:v1.0.1
docker push ghcr.io/albertobasso/hsync-server:v1.0.1
docker tag hsync-server:latest ghcr.io/albertobasso/hsync-server:latest
docker push ghcr.io/albertobasso/hsync-server:latest
```

### Create GitHub Release

1. Go to [Releases](https://github.com/albertobasso/hsync-server/releases)
2. Click "Draft a new release"
3. Tag: `v1.0.1`
4. Title: `v1.0.1`
5. Click "Generate release notes" for the auto-generated changelog
6. Publish

### Post-release checklist

- [ ] Docker image is available on GHCR
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
