"""
Pytest fixtures for hsync-server tests.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Set test configuration before importing app
os.environ["HSYNC_API_KEY"] = "test-api-key-12345"


@pytest.fixture(autouse=True)
def _prevent_os_environ_leakage(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Snapshot ``os.environ`` before each test and remove any keys added
    by the test afterwards.

    Tests that manipulate ``os.environ`` directly (without ``monkeypatch``)
    can leak variables into later tests.  This fixture acts as a safety net:
    it records the full set of keys before the test and deletes any new keys
    that appear when the test finishes.
    """
    before = set(os.environ.keys())
    yield
    after = set(os.environ.keys())
    added = after - before
    for key in sorted(added):
        monkeypatch.delenv(key, raising=False)


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
        patch("hsync_server.api.files.list.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delete.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delta.apply.get_buckets_path", return_value=temp_buckets_dir),
        patch(
            "hsync_server.api.files.delta.tokens.get_buckets_path", return_value=temp_buckets_dir
        ),
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
            delta_apply,
            download_file,
            get_delta_data,
            get_delta_signatures,
            get_files,
            get_tombstones,
            get_upload_status,
            post_delta_signatures,
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
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/delta/signatures",
            get_delta_signatures,
            methods=["GET"],
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/delta/signatures",
            post_delta_signatures,
            methods=["POST"],
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/delta/apply", delta_apply, methods=["POST"]
        )
        test_auth_router.add_api_route(
            "/api/v1/buckets/{bucket}/files/delta/data/{token}", get_delta_data, methods=["GET"]
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
        patch("hsync_server.api.files.list.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.files.delete.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.api.buckets.get_buckets_path", return_value=temp_buckets_dir),
        patch("hsync_server.tombstones.get_buckets_path", return_value=temp_buckets_dir),
    ):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# =============================================================================
# Black-box harness (test_bb_*.py)
#
# Behavioural, use-case-driven tests drive the *real* server over loopback HTTP
# and WebSocket. To stay isolated from this repo's in-process config tests
# (which mutate ``HSYNC_*`` env and clear the settings cache mid-session), the
# live server runs as a **subprocess** with its own process state. No internal
# logic is mocked: only the filesystem (a temp buckets dir) and the API key are
# test fixtures. See ``.kilo/plans/1786000181361-blackbox-test-suite.md``.
#
# CI strategy note (deliberately divergent from hsync-client): the client
# excludes its bb suite from CI (--ignore-glob='*test_bb*') because its
# watcher/inotify timing races make it flaky; every test in THIS suite drives
# a deterministic request/response surface, so it runs in CI. Do not
# "harmonize" the two without understanding that difference.
# =============================================================================

# A fixed, strong API key for every black-box run (>= 16 chars per Settings).
BB_API_KEY = "test-bb-api-key-0123456789abcdef"
BB_CLIENT_CID = "TestClientX1234567890"  # exactly 21 alphanumeric chars per CID_PATTERN


def _free_port() -> int:
    """Reserve and release an ephemeral loopback port for the server to bind."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _log_tail(log_path: str, limit: int = 8192) -> str:
    """Return the last ``limit`` bytes of the server log (for failure reports)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # SEEK_END
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return "<server log unavailable>"


def _discard_file(path: str) -> None:
    """Best-effort removal of the server log file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _start_live_server(
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, int, str, str]:
    """Start the real hsync-server on an ephemeral loopback port.

    Returns ``(process, port, buckets_dir, log_path)``. The caller owns
    termination and cleanup (of both the buckets dir and the log file).
    Raises ``RuntimeError`` if the server does not become healthy.

    The server log goes to a temp file, never a pipe: a ``PIPE`` that nobody
    drains after startup would fill its ~64 KB kernel buffer over a session
    (every intentionally-triggered 4xx logs a warning line, every 5xx a full
    traceback) and then *block the subprocess on write*, hanging the whole
    suite with no per-test timeout to catch it.
    """
    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_buckets_")
    port = _free_port()
    env = {
        **os.environ,
        "HSYNC_API_KEY": BB_API_KEY,
        "HSYNC_BUCKETS": buckets_dir,
        # Raise limits so the behavioural suite never trips a 429; individual
        # rate-limit tests spawn a dedicated low-limit server instead.
        "HSYNC_RATE_LIMIT_DEFAULT": "1000000/minute",
        "HSYNC_RATE_LIMIT_UPLOAD": "1000000/minute",
        "HSYNC_RATE_LIMIT_DOWNLOAD": "1000000/minute",
        "HSYNC_RATE_LIMIT_DELTA": "1000000/minute",
        "HSYNC_RATE_LIMIT_WEBSOCKET": "1000000/minute",
        "HSYNC_RATE_LIMIT_WS_MESSAGES": "1000000/second",
        "HSYNC_WS_AUTH_TIMEOUT": "5",
        "HSYNC_WS_HEARTBEAT_INTERVAL": "5",
        "HSYNC_WS_HEARTBEAT_TIMEOUT": "3",
        "HSYNC_MIN_FREE_DISK_SPACE": "0",
        "HSYNC_ENABLE_DOCS": "false",
    }
    if env_overrides:
        env.update(env_overrides)

    log_fd, log_path = tempfile.mkstemp(prefix="hsync_bb_server_", suffix=".log")
    log_file = os.fdopen(log_fd, "wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hsync_server", "--port", str(port), "--log-level", "warning"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        # The subprocess holds its own duplicate of the fd; the parent's copy
        # must be closed so the log can be read and removed later.
        log_file.close()

    import httpx

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    last_exc: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            shutil.rmtree(buckets_dir, ignore_errors=True)
            _discard_file(log_path)
            raise RuntimeError(f"hsync-server exited early (code {proc.returncode}):\n{tail}")
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return proc, port, buckets_dir, log_path
        except Exception as exc:  # noqa: BLE001 - startup not ready yet
            last_exc = exc
        time.sleep(0.15)
    _stop_proc(proc)
    tail = _log_tail(log_path)
    shutil.rmtree(buckets_dir, ignore_errors=True)
    _discard_file(log_path)
    raise RuntimeError(
        f"hsync-server did not become healthy on port {port}: {last_exc}\nserver log tail:\n{tail}"
    )


def _stop_proc(proc: subprocess.Popen) -> None:
    """Terminate and reap a server subprocess, killing if needed."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def live_server() -> Generator[tuple[str, str]]:
    """Module-scoped real loopback server: yields (base_url, api_key)."""
    proc, port, buckets_dir, log_path = _start_live_server()
    try:
        yield f"http://127.0.0.1:{port}", BB_API_KEY
    finally:
        _stop_proc(proc)
        shutil.rmtree(buckets_dir, ignore_errors=True)
        _discard_file(log_path)


@pytest.fixture
def start_live_server():
    """Expose a context-managed starter for tests needing a custom-config server.

    Returns a callable ``start(env_overrides=None)`` that yields
    ``(base_url, api_key)`` and guarantees process termination + temp dir
    cleanup on exit, so callers never need to manage teardown themselves.
    """
    import contextlib

    @contextlib.contextmanager
    def _start(env_overrides: dict[str, str] | None = None):
        proc, port, buckets_dir, log_path = _start_live_server(env_overrides)
        try:
            yield f"http://127.0.0.1:{port}", BB_API_KEY
        finally:
            _stop_proc(proc)
            shutil.rmtree(buckets_dir, ignore_errors=True)
            _discard_file(log_path)

    return _start


@pytest.fixture
def bb_http(live_server: tuple[str, str]) -> Generator[httpx.Client]:
    """Synchronous HTTP client authenticated against the live server."""
    import httpx

    base_url, api_key = live_server
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture
def bb_bucket(bb_http: httpx.Client) -> Generator[str]:
    """Create a uniquely-named bucket for one test; remove it afterwards."""
    import uuid

    name = f"bb-{uuid.uuid4().hex[:10]}"
    r = bb_http.post("/api/v1/buckets", params={"bucket": name})
    assert r.status_code == 201, r.text
    yield name
    bb_http.delete(f"/api/v1/buckets/{name}")


@pytest.fixture
def bb_upload():
    """Helper to upload a file to the live server over HTTP (black-box)."""
    import hashlib

    def _upload(
        client: httpx.Client,
        bucket: str,
        filename: str,
        content: bytes,
        *,
        path: str = "",
        sha256sum: str | None = None,
        permissions: str = "644",
        offset: int = 0,
        size: int = 0,
        compressed: str = "false",
        cid: str = BB_CLIENT_CID,
    ) -> httpx.Response:
        return client.post(
            f"/api/v1/buckets/{bucket}/files",
            data={
                "cid": cid,
                "path": path,
                "sha256sum": sha256sum
                if sha256sum is not None
                else hashlib.sha256(content).hexdigest(),
                "permissions": permissions,
                "offset": str(offset),
                "size": str(size),
                "compressed": compressed,
            },
            files={"file": (filename, content, "application/octet-stream")},
        )

    return _upload
