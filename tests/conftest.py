"""
Pytest fixtures for hsync-server tests.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Set test configuration before importing app.
# HSYNC_ADMIN_KEY is the rendezvous admin secret (64 hex). The Bearer token is
# hs_HsyncAdminRendezvous01_<secret> (constructed below as TEST_TOKEN).
TEST_ADMIN_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TEST_TOKEN = f"hs_HsyncAdminRendezvous01_{TEST_ADMIN_SECRET}"
os.environ["HSYNC_ADMIN_KEY"] = TEST_ADMIN_SECRET

# ``HSYNC_*`` keys the import-time scrub must NOT delete. ``HSYNC_ADMIN_KEY``
# is set above for the rendezvous admin account; ``HSYNC_BB_SERVER`` /
# ``HSYNC_BB_LAN`` are test opt-in flags (read by the ``test_bb_*`` modules at
# import time) and are not ``Settings`` fields, so a shell-exported value must
# survive to collection or the whole black-box suite silently skips.
_SCRUB_EXEMPT_HSYNC_KEYS = frozenset(
    {"HSYNC_ADMIN_KEY", "HSYNC_BB_SERVER", "HSYNC_BB_LAN", "HSYNC_BB_SECURITY"}
)

# Scrub any HSYNC_* inherited from the developer's shell at conftest import
# time — BEFORE the first test module is collected. Contract tests import
# ``hsync_server.main`` at module level, and that import already builds a
# ``Settings`` instance (rate_limiter module constants) whose legacy-key guard
# aborts collection in a shell that still exports the removed ``HSYNC_API_KEY``
# (dev.env), and whose values are then baked into the ``@lru_cache`` of
# ``get_settings``. The autouse ``_scrub_hsync_env`` fixture runs only after
# collection — too late for this. Process-local: the caller's shell env is
# untouched.
for _leaked in [
    k for k in os.environ if k.startswith("HSYNC_") and k not in _SCRUB_EXEMPT_HSYNC_KEYS
]:
    del os.environ[_leaked]


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
def _scrub_hsync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub every ``HSYNC_*`` inherited from the developer's environment.

    ``Settings`` (pydantic-settings) reads the server's configuration from
    ``HSYNC_*`` env vars, so a shell-exported ``HSYNC_MAX_UPLOAD_SIZE`` /
    ``HSYNC_RATE_LIMIT_*`` / … would otherwise leak into default-value
    assertions (``test_default_values``) and make them depend on the execution
    environment. ``monkeypatch.delenv`` restores the original values on
    teardown; tests that set their own ``HSYNC_*`` (via ``monkeypatch.setenv``
    or directly) still work because they run after this scrub.

    ``HSYNC_ADMIN_KEY`` is excluded: the conftest sets it at import time for
    the rendezvous admin account, so it must survive the scrub.
    """
    for key in [k for k in os.environ if k.startswith("HSYNC_") and k != "HSYNC_ADMIN_KEY"]:
        monkeypatch.delenv(key)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_STATE_DIR = _REPO_ROOT / "state"
_REAL_BUCKETS_DIR = _REPO_ROOT / "buckets"


def _real_db_snapshot() -> tuple[tuple[str, tuple[int, int]], ...] | None:
    """Snapshot the repo's real ``state/server/`` DB files (name + size + mtime).

    Returns ``None`` when the directory does not exist (nothing to protect).
    Read-only: only ``stat``, never opens the files, so taking the snapshot can
    never create ``-wal``/``-shm`` side files in the real state dir.
    """
    directory = _REAL_STATE_DIR / "server"
    if not directory.is_dir():
        return None
    try:
        entries = []
        for path in sorted(directory.rglob("*.db")):
            st = path.stat()
            entries.append((str(path.relative_to(_REAL_STATE_DIR)), (st.st_size, st.st_mtime_ns)))
        return tuple(entries)
    except OSError:  # pragma: no cover - defensive
        return None


def _real_buckets_snapshot() -> frozenset[str]:
    """Return the set of directory names currently in the repo's real buckets dir."""
    if not _REAL_BUCKETS_DIR.exists():
        return frozenset()
    return frozenset(entry.name for entry in _REAL_BUCKETS_DIR.iterdir() if entry.is_dir())


@pytest.fixture(autouse=True)
def no_tests_touch_real_repo_state(
    request: pytest.FixtureRequest, fast_scrypt: None
) -> Generator[None]:
    """Fail loudly if a test writes to the repository's real state/buckets.

    The ``fast_scrypt`` fixture isolates every in-process test to a temporary
    state dir and buckets dir; the black-box harness scrubs ``HSYNC_*`` and
    points the subprocess server at temp dirs too. This fixture is the safety
    net for the residual risk: if any code path still resolves to the repo's
    real ``state/server/`` DBs (or creates directories under ``buckets/``),
    the logical snapshot changes and the offending test fails instead of
    silently leaving test buckets behind (see the ``bb-*`` ghost rows that
    motivated it).
    """
    before_db = _real_db_snapshot()
    before_buckets = _real_buckets_snapshot()
    yield
    after_db = _real_db_snapshot()
    after_buckets = _real_buckets_snapshot()
    if after_db != before_db:
        pytest.fail(
            f"{request.node.nodeid} mutated the repository's real "
            f"{_REAL_STATE_DIR}/server/ state DBs. "
            "Tests must use the isolated tmp dirs provided by the fast_scrypt fixture "
            "or scrub HSYNC_* in subprocess servers."
        )
    if after_buckets != before_buckets:
        pytest.fail(
            f"{request.node.nodeid} created or removed directories under the repository's real "
            f"{_REAL_BUCKETS_DIR}. Tests must use the isolated tmp dirs provided by the "
            "fast_scrypt fixture or scrub HSYNC_* in subprocess servers."
        )


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


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Use low scrypt parameters, isolate state dir, and rendezvous admin.

    Combines scrypt patching, state dir isolation, and admin rendezvous into a
    single fixture to ensure correct ordering.
    """
    import secrets as _secrets

    from hsync_server import auth_store
    from hsync_server.auth_store import credentials as _auth_credentials

    # --- Low scrypt params ---
    # The scrypt parameters live in the ``credentials`` submodule and are read
    # at call time by _hash_secret/_verify_secret, so patch them there.
    monkeypatch.setattr(_auth_credentials, "SCRYPT_N", 2**4)
    monkeypatch.setattr(_auth_credentials, "SCRYPT_R", 8)
    monkeypatch.setattr(_auth_credentials, "SCRYPT_P", 1)
    monkeypatch.setattr(_auth_credentials, "SCRYPT_MAXMEM", 32 * 1024 * 1024)
    dummy_salt = _secrets.token_bytes(_auth_credentials.SALT_LEN)
    # _DUMMY_PHC is the PHC string auth._dummy_verify reads; rebuild it with
    # the patched low scrypt params so the anti-enumeration burn stays fast.
    # ``hsync_server.auth`` imported the value at import time, so patch it there
    # (and on the facade) as well as on the defining submodule.
    dummy_phc = _auth_credentials._hash_secret("hsync-dummy-key-for-timing", dummy_salt)
    monkeypatch.setattr(_auth_credentials, "_DUMMY_PHC", dummy_phc)
    monkeypatch.setattr(auth_store, "_DUMMY_PHC", dummy_phc)
    import hsync_server.auth as _auth_module

    monkeypatch.setattr(_auth_module, "_DUMMY_PHC", dummy_phc, raising=False)

    # --- Isolate state dir and buckets ---
    tmp = tmp_path_factory.mktemp("hsync_test")
    state = str(tmp / "state")
    buckets = str(tmp / "buckets")
    os.makedirs(state, exist_ok=True)
    os.makedirs(buckets, exist_ok=True)

    # Patch get_state_dir and get_buckets_path everywhere they're imported.
    for mod_name, attr, val in [
        ("hsync_server.config", "get_state_dir", lambda: state),
        ("hsync_server.config", "get_buckets_path", lambda: buckets),
        ("hsync_server.auth", "get_buckets_path", lambda: buckets),
        ("hsync_server.versions", "get_state_dir", lambda: state),
        ("hsync_server.api.authz", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.buckets", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.utils", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.events", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.members", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.upload", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.list", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.permissions", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.delete", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.rename", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.download", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.delta.apply", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.files.delta.tokens", "get_buckets_path", lambda: buckets),
        ("hsync_server.api.admin", "get_buckets_path", lambda: buckets),
        ("hsync_server.bucket_provisioning", "get_buckets_path", lambda: buckets),
    ]:
        try:
            mod = __import__(mod_name, fromlist=[attr])
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val)
        except ImportError:
            pass

    # --- Clear caches ---
    from hsync_server.auth import reset_auth_cache as _reset_auth_cache

    _reset_auth_cache()
    auth_store.reset_auth_cache()
    from hsync_common.models.sqlstore import reset_state_db_cache as _reset_state_db

    _reset_state_db()
    from hsync_server.versions import reset_cache as _reset_versions

    _reset_versions()
    try:
        from hsync_server.tombstones import reset_cache as _reset_tombstones

        _reset_tombstones()
    except ImportError:
        pass
    # Clear settings cache so env changes take effect.
    from hsync_server.config import get_settings as _get_settings

    _get_settings.cache_clear()

    # --- Rendezvous admin ---
    store = auth_store.get_auth_store()
    store.rendezvous_admin(TEST_ADMIN_SECRET)


@pytest.fixture()
def test_token() -> str:
    """Return the test admin Bearer token."""
    return TEST_TOKEN


@pytest.fixture()
def test_headers() -> dict[str, str]:
    """Return Authorization headers with the test admin token."""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def temp_buckets_dir() -> Generator[str]:
    """Create a temporary directory for test buckets (legacy compat)."""
    from hsync_server.config import get_buckets_path

    yield get_buckets_path()


@pytest.fixture
def test_app() -> Generator[FastAPI]:
    """Create a test FastAPI application.

    State dir and buckets path are already patched by the ``fast_scrypt``
    autouse fixture; this fixture only assembles the router/handler stack.
    """
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    from hsync_server.error_handlers import register_exception_handlers
    from hsync_server.rate_limiter import limiter, rate_limit_exceeded_handler

    app = FastAPI(title="HSYNC Test")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    register_exception_handlers(app)

    # Create new routers for test app (avoid reusing global routers)
    from fastapi import APIRouter, Depends

    from hsync_server.auth import verify_api_key

    test_public_router = APIRouter(tags=["Public Router"])
    test_auth_router = APIRouter(tags=["Auth Router"], dependencies=[Depends(verify_api_key)])

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
        get_dirs,
        get_files,
        get_tombstones,
        get_upload_status,
        patch_file_permissions,
        post_delta_signatures,
        rename_file,
        upload_file,
    )

    # Import presence endpoint from presence module
    from hsync_server.api.presence import get_presence

    test_auth_router.add_api_route("/api/v1/buckets/{bucket}", get_bucket, methods=["GET"])
    test_auth_router.add_api_route("/api/v1/buckets", create_bucket, methods=["POST"])
    test_auth_router.add_api_route("/api/v1/buckets/{bucket}", delete_bucket, methods=["DELETE"])
    test_auth_router.add_api_route(
        "/api/v1/buckets/{bucket}/presence", get_presence, methods=["GET"]
    )
    test_auth_router.add_api_route("/api/v1/buckets/{bucket}/files", upload_file, methods=["POST"])
    test_auth_router.add_api_route(
        "/api/v1/buckets/{bucket}/files/download", download_file, methods=["GET"]
    )
    test_auth_router.add_api_route("/api/v1/buckets/{bucket}/files", get_files, methods=["GET"])
    test_auth_router.add_api_route(
        "/api/v1/buckets/{bucket}/files/upload-status", get_upload_status, methods=["GET"]
    )
    test_auth_router.add_api_route("/api/v1/buckets/{bucket}/dirs", get_dirs, methods=["GET"])
    test_auth_router.add_api_route(
        "/api/v1/buckets/{bucket}/files/permissions", patch_file_permissions, methods=["PATCH"]
    )
    test_auth_router.add_api_route(
        "/api/v1/buckets/{bucket}/files", delete_file, methods=["DELETE"]
    )
    test_auth_router.add_api_route("/api/v1/buckets/{bucket}/files", rename_file, methods=["PATCH"])
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


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Create an async test client with admin auth headers."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
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

# A fixed admin secret (64 hex) for every black-box run. The Bearer token is
# hs_HsyncAdminRendezvous01_<secret>.
BB_ADMIN_SECRET = "bb0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
BB_API_KEY = f"hs_HsyncAdminRendezvous01_{BB_ADMIN_SECRET}"  # full Bearer token
BB_CLIENT_CID = "TestClientX1234567890"  # exactly 21 alphanumeric chars per CID_PATTERN
# A second, distinct client id so black-box tests can author *concurrent*
# causal histories (two different counters on the same path).
BB_CLIENT_CID_B = "TestClientY1234567890"


# ---------------------------------------------------------------------------
# Black-box test-data helpers
#
# ``bb_sample`` builds reproducible random artefacts (file name + payload +
# seed). Every generated sample is recorded on the test node so that, on
# failure, its name and seed are appended to the report for reproducibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBSample:
    """A reproducible random test artefact."""

    name: str
    payload: bytes
    seed: int

    @property
    def ctx(self) -> str:
        """Context string for reproducibility logs."""
        return f"[bb-sample] name={self.name} seed={self.seed}"


@pytest.fixture
def bb_sample(request: pytest.FixtureRequest):
    """Return a factory that builds reproducible random test data.

    Usage::

        s = bb_sample(size=1024)
        r = bb_upload(client, bucket, s.name, s.payload)
        ... assert ..., s.ctx
    """
    import random
    import uuid

    recorded: list[BBSample] = []
    request.node._bb_samples = recorded  # type: ignore[attr-defined]

    def _make(suffix: str = ".txt", size: int = 0, prefix: str = "bb") -> BBSample:
        name = f"{prefix}-{uuid.uuid4().hex[:8]}{suffix}"
        seed = random.randrange(2**32)
        payload = random.Random(seed).randbytes(size) if size else b""
        sample = BBSample(name=name, payload=payload, seed=seed)
        recorded.append(sample)
        return sample

    return _make


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """On failure, append recorded black-box sample seeds to the report.

    Uses ``hookwrapper=True`` so the default ``TestReport`` is still created
    by pytest's built-in implementation — a plain ``firstresult`` override
    would shadow it and break report-dependent fixture teardown.
    """
    outcome = yield
    if call.when == "call":
        report = outcome.get_result()
        if report.failed:
            samples = getattr(item, "_bb_samples", None)
            if samples:
                report.sections.append(
                    (
                        "bb-samples",
                        "Black-box samples (name + seed for reproducibility):\n"
                        + "\n".join(s.ctx for s in samples),
                    )
                )


# Live-server startup attempts. The port is reserved with a short-lived probe
# socket that must be closed before the server can bind it, so an outgoing
# connection can still take the port in that window; the server then exits with
# EADDRINUSE and the attempt is retried on a fresh port.
_START_ATTEMPTS = 3
_BIND_CONFLICT_MARKERS = ("address already in use", "errno 98", "eaddrinuse")


def _free_port() -> int:
    """Reserve and release an ephemeral port for the server's wildcard bind.

    The probe binds ``0.0.0.0`` — what ``hsync_server`` binds by default — not
    loopback: an ephemeral port free on ``127.0.0.1`` can already be taken on
    another local address, in which case the server's wildcard bind fails.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _is_bind_conflict(log_tail: str) -> bool:
    """True when *log_tail* shows the bind failing because the port is taken."""
    lowered = log_tail.lower()
    return any(marker in lowered for marker in _BIND_CONFLICT_MARKERS)


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


def _wait_for_server_ready(
    proc: subprocess.Popen,
    base_url: str,
    log_path: str,
    timeout: float = 30.0,
    verify: bool = True,
) -> None:
    """Poll the server's ``/health`` endpoint until it responds 200.

    Raises ``RuntimeError`` if the server exits prematurely or does not
    become healthy within *timeout* seconds.  When *verify* is ``False``
    the probe skips TLS certificate verification (self-signed certs).
    """
    import httpx

    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            raise RuntimeError(f"hsync-server exited early (code {proc.returncode}):\n{tail}")
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0, verify=verify)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - startup not ready yet
            last_exc = exc
        time.sleep(0.15)
    tail = _log_tail(log_path)
    raise RuntimeError(
        f"hsync-server did not become healthy at {base_url}: {last_exc}\nserver log tail:\n{tail}"
    )


def _start_live_server(
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, str, str, str]:
    """Start the real hsync-server on an ephemeral loopback port.

    Returns ``(process, base_url, buckets_dir, log_path)``. The caller owns
    termination and cleanup (of both the buckets dir and the log file).
    Raises ``RuntimeError`` if the server does not become healthy.

    A start that loses the port race (EADDRINUSE, see :data:`_START_ATTEMPTS`)
    is retried on a fresh port before the failure is reported.

    When *env_overrides* contains ``HSYNC_SSL_ENABLED=True`` the server is
    started with TLS and the readiness probe uses ``https://`` with
    ``verify=False`` (self-signed cert).

    The server log goes to a temp file, never a pipe: a ``PIPE`` that nobody
    drains after startup would fill its ~64 KB kernel buffer over a session
    (every intentionally-triggered 4xx logs a warning line, every 5xx a full
    traceback) and then *block the subprocess on write*, hanging the whole
    suite with no per-test timeout to catch it.
    """
    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_buckets_")
    # Scrub pre-existing HSYNC_* variables so the developer's own configuration
    # can never leak into the test server, then apply the test overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(
        {
            "HSYNC_ADMIN_KEY": BB_ADMIN_SECRET,
            "HSYNC_SCRYPT_N": "16",
            "HSYNC_SCRYPT_MAXMEM": "33554432",
            "HSYNC_BUCKETS": buckets_dir,
            "HSYNC_STATE_DIR": buckets_dir + "_state",
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
    )
    if env_overrides:
        env.update(env_overrides)

    use_ssl = bool(
        env_overrides and env_overrides.get("HSYNC_SSL_ENABLED", "").lower() in ("true", "1", "yes")
    )
    scheme = "https" if use_ssl else "http"

    last_error: RuntimeError | None = None
    for _attempt in range(_START_ATTEMPTS):
        port = _free_port()
        base_url = f"{scheme}://127.0.0.1:{port}"

        log_fd, log_path = tempfile.mkstemp(prefix="hsync_bb_server_", suffix=".log")
        log_file = os.fdopen(log_fd, "wb")
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_server",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        finally:
            # The subprocess holds its own duplicate of the fd; the parent's copy
            # must be closed so the log can be read and removed later.
            log_file.close()

        try:
            _wait_for_server_ready(proc, base_url, log_path, verify=not use_ssl)
        except RuntimeError as exc:
            last_error = exc
            _stop_proc(proc)
            _discard_file(log_path)
            # The message carries the log tail, so the discarded file loses
            # nothing. A bind conflict is a lost port race, not a server defect:
            # retry on a fresh port. Any other startup failure is reported now.
            if not _is_bind_conflict(str(exc)):
                break
            continue
        return proc, base_url, buckets_dir, log_path

    # No attempt could start: release the caller-owned resources and report.
    shutil.rmtree(buckets_dir, ignore_errors=True)
    shutil.rmtree(buckets_dir + "_state", ignore_errors=True)
    raise last_error or RuntimeError("hsync-server did not start")


def _stop_proc(proc: subprocess.Popen) -> None:
    """Terminate and reap a server subprocess, killing if needed."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _terminate_orphan_server_processes() -> None:
    """Best-effort reap of ``hsync_server`` subprocesses still parented to pytest.

    Session-scoped teardown runs *after* every module-scoped ``live_server`` has
    been stopped (module fixtures finalize first), so any surviving child is a
    leftover from a crashed test, never a live fixture — safe to terminate.
    POSIX-only (``pgrep`` + ``/proc``); a no-op where either is unavailable.
    """
    if not shutil.which("pgrep"):
        return
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return
    for token in result.stdout.split():
        if not token.isdigit():
            continue
        pid = int(token)
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"hsync_server" not in cmdline and b"hsync-server" not in cmdline:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue


@pytest.fixture(autouse=True, scope="session")
def _reap_orphan_server_processes() -> Generator[None]:
    """Defense-in-depth: reap orphaned ``hsync_server`` processes at session end."""
    yield
    _terminate_orphan_server_processes()


@pytest.fixture(scope="module")
def live_server() -> Generator[tuple[str, str]]:
    """Module-scoped real loopback server: yields (base_url, api_key)."""
    proc, base_url, buckets_dir, log_path = _start_live_server()
    try:
        yield base_url, BB_API_KEY
    finally:
        _stop_proc(proc)
        shutil.rmtree(buckets_dir, ignore_errors=True)
        shutil.rmtree(buckets_dir + "_state", ignore_errors=True)
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
        proc, base_url, buckets_dir, log_path = _start_live_server(env_overrides)
        try:
            yield base_url, BB_API_KEY
        finally:
            _stop_proc(proc)
            shutil.rmtree(buckets_dir, ignore_errors=True)
            shutil.rmtree(buckets_dir + "_state", ignore_errors=True)
            _discard_file(log_path)

    return _start


@pytest.fixture
def restartable_live_server():
    """A restartable live server whose buckets/state survive ``restart()``.

    Yields a callable ``restart(env_overrides=None)`` used as a context manager:
    each call starts a **fresh** server process bound to the *same* buckets and
    state directories as the previous call, so on-disk state persists across a
    simulated restart, and yields ``(base_url, api_key)``. Termination and the
    throwaway temp dir are cleaned up on exit; the shared dirs are removed at
    fixture end.
    """
    import contextlib

    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_restart_buckets_")
    state_dir = tempfile.mkdtemp(prefix="hsync_bb_restart_state_")
    shared_env = {"HSYNC_BUCKETS": buckets_dir, "HSYNC_STATE_DIR": state_dir}

    @contextlib.contextmanager
    def _restart(env_overrides: dict[str, str] | None = None):
        env = dict(shared_env)
        if env_overrides:
            env.update(env_overrides)
        # ``_start_live_server`` creates a throwaway buckets dir it expects the
        # caller to remove; the env overrides point the server at the shared
        # dirs above, so the throwaway one is never written to.
        proc, base_url, throwaway_dir, log_path = _start_live_server(env)
        try:
            yield base_url, BB_API_KEY
        finally:
            _stop_proc(proc)
            shutil.rmtree(throwaway_dir, ignore_errors=True)
            shutil.rmtree(throwaway_dir + "_state", ignore_errors=True)
            _discard_file(log_path)

    try:
        yield _restart
    finally:
        shutil.rmtree(buckets_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)


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
        vclock: str = "",
        version_id: str = "",
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
                "vclock": vclock,
                "version_id": version_id,
            },
            files={"file": (filename, content, "application/octet-stream")},
        )

    return _upload
