"""
Pytest fixtures for hsync-server tests.
"""

import os
import tempfile
from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Set test configuration before importing app
os.environ["HSYNC_API_KEY"] = "test-api-key-12345"


@pytest.fixture(autouse=True)
def deterministic_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Force plain, uncolored CLI output for every test.

    Typer forces a colored terminal whenever ``GITHUB_ACTIONS`` (or
    ``FORCE_COLOR``/``PY_COLORS``) is set, so on CI its Rich highlighter wraps
    option names like ``--log-level`` in ANSI codes, splitting the substring and
    breaking assertions on ``result.output``/``result.stdout``. That decision is
    baked into ``typer.rich_utils.FORCE_TERMINAL`` at import time, so clearing
    the env vars later is useless; instead we override the module global, which
    ``_get_rich_console`` re-reads on every call. ``False`` disables color/style
    entirely while still honouring an explicit ``COLUMNS``.
    """
    import typer.rich_utils as rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False, raising=False)


@pytest.fixture
def temp_buckets_dir() -> Generator[str]:
    """Create a temporary directory for test buckets."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def test_app(temp_buckets_dir: str) -> Generator[FastAPI]:
    """Create a test FastAPI application with temp buckets directory."""
    # Redirect get_buckets_path() to the temp directory in all modules that import it
    with (
        patch("hsync_server.api.utils.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.upload.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.download.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.list.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delete.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.buckets.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.tombstones.get_buckets_path", return_value=temp_buckets_dir),
    ):
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        from hsync_server.error_handlers import register_exception_handlers
        from hsync_server.rate_limiter import limiter, rate_limit_exceeded_handler

        # Create a fresh app for testing
        app = FastAPI(title="HSYNC Test")
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        # Register custom exception handlers
        register_exception_handlers(app)

        # Create new routers for test app (avoid reusing global routers)
        from fastapi import APIRouter, Depends

        from hsync_server.routers import verify_api_key

        test_public_router = APIRouter(tags=["Public Router"])
        test_auth_router = APIRouter(tags=["Auth Router"], dependencies=[Depends(verify_api_key)])

        # Register health endpoint
        @test_public_router.get("/health")
        async def health():
            return ""

        # Import bucket endpoints from buckets module
        from hsync_server.api.buckets import create_bucket, delete_bucket, get_bucket

        # Import file endpoints from files module
        from hsync_server.api.files import (
            delete_file,
            download_file,
            get_files,
            get_tombstones,
            get_upload_status,
            upload_file,
        )

        test_auth_router.add_api_route("/api/v1/buckets/{bucket}", get_bucket, methods=["GET"])
        test_auth_router.add_api_route("/api/v1/buckets", create_bucket, methods=["POST"])
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}", delete_bucket, methods=["DELETE"]
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files", upload_file, methods=["POST"]
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/download", download_file, methods=["GET"]
        )
        test_auth_router.add_api_route("/api/v1/buckets/{bucket}/files", get_files, methods=["GET"])
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/upload-status", get_upload_status, methods=["GET"]
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files", delete_file, methods=["DELETE"]
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/tombstones", get_tombstones, methods=["GET"]
        )

        app.include_router(test_public_router)
        app.include_router(test_auth_router)

        yield app


@pytest.fixture(autouse=True)
def reset_tombstone_cache() -> Generator[None]:
    """Clear the per-bucket tombstone store cache so tests stay isolated."""
    from hsync_server.tombstones import reset_cache

    reset_cache()
    yield
    reset_cache()


@pytest.fixture
async def client(test_app: FastAPI, temp_buckets_dir: str) -> AsyncGenerator[AsyncClient]:
    """Create an async test client."""
    with (
        patch("hsync_server.api.files.upload.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.download.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.list.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delete.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.buckets.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.tombstones.get_buckets_path", return_value=temp_buckets_dir),
    ):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key-12345"},
        ) as ac:
            yield ac


@pytest.fixture
async def client_no_auth(test_app: FastAPI, temp_buckets_dir: str) -> AsyncGenerator[AsyncClient]:
    """Create an async test client without authentication."""
    with (
        patch("hsync_server.api.files.upload.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.download.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.list.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delete.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.buckets.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.tombstones.get_buckets_path", return_value=temp_buckets_dir),
    ):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
