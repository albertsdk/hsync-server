# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The admin REST surface of the server history store (ADR-0057).

A deployment run as a container or appliance has no shell for ``hsync-server
admin history``, so the same list/restore operations are reachable over the admin
API. These tests drive the endpoints over ASGI and check the contract an operator
sees: the preserved record shape, the status code each restore outcome reports,
and the authorization the router already demands.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hsync_server import history as history_mod
from hsync_server.api import admin as admin_api
from hsync_server.error_handlers import register_exception_handlers
from tests.history_helpers import age_entry

BUCKET = "photos"


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """Point the store at a temporary state dir (the real resolution path)."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(history_mod, "get_state_dir", lambda: str(state))
    return state


@pytest.fixture
def bucket_dir() -> Path:
    """Create the served bucket directory both endpoints check for."""
    root = Path(admin_api.get_buckets_path()) / BUCKET
    root.mkdir(parents=True, exist_ok=True)
    return root


def _admin_app() -> FastAPI:
    """Assemble a bare app mounting the admin router with its error handlers."""
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    from hsync_server.rate_limiter import limiter, rate_limit_exceeded_handler

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    register_exception_handlers(app)
    app.include_router(admin_api.admin_router)
    return app


@pytest.fixture
async def admin_client(test_headers) -> AsyncGenerator[AsyncClient]:
    """A client mounting only the admin router, with the built-in admin token."""
    async with AsyncClient(
        transport=ASGITransport(app=_admin_app()),
        base_url="http://test",
        headers=test_headers,
    ) as client:
        yield client


def _write(path: Path, payload: bytes = b"content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _replace(path: Path, payload: bytes) -> Path:
    """Change a live file the way every writer does: a new inode, atomically.

    Replacing in place would also rewrite the preserved copy, because capture
    hard-links the live file instead of copying its bytes.
    """
    staged = path.with_name(path.name + ".staged")
    staged.write_bytes(payload)
    os.replace(staged, path)
    return path


def _capture(bucket_dir: Path, relpath: str, payload: bytes = b"content") -> str:
    """Preserve *relpath* as the overwrite hook does; return its entry id."""
    from hsync_common.constants import HISTORY_REASON_OVERWRITE

    entry_id = history_mod.capture_file(
        BUCKET,
        str(_write(bucket_dir / relpath, payload)),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_OVERWRITE,
    )
    assert entry_id is not None
    return entry_id


def _list_url(bucket: str = BUCKET) -> str:
    return admin_api.REST_ADMIN_BUCKET_HISTORY_PATH.format(bucket=bucket)


def _restore_url(entry_id: str, bucket: str = BUCKET) -> str:
    return admin_api.REST_ADMIN_BUCKET_HISTORY_RESTORE_PATH.format(bucket=bucket, entry_id=entry_id)


# ── listing ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_empty_store_lists_nothing(admin_client, state_dir, bucket_dir) -> None:
    response = await admin_client.get(_list_url())

    assert response.status_code == 200
    assert response.json() == {"bucket": BUCKET, "entries": [], "storage_bytes": 0}


@pytest.mark.asyncio
async def test_the_listing_reports_the_preserved_versions(
    admin_client, state_dir, bucket_dir
) -> None:
    older = _capture(bucket_dir, "docs/report.txt", b"first draft")
    newer = _capture(bucket_dir, "photo.jpg", b"jpeg bytes")
    age_entry(state_dir, BUCKET, older, 1)

    response = await admin_client.get(_list_url())

    assert response.status_code == 200
    body = response.json()
    assert [item["entry_id"] for item in body["entries"]] == [newer, older]
    item = body["entries"][1]
    assert item["path"] == "docs/report.txt"
    assert item["size"] == len(b"first draft")
    assert item["reason"] == "overwrite"
    assert item["cid"] is None
    assert item["recorded_at"] > 0
    assert body["storage_bytes"] == len(b"first draft") + len(b"jpeg bytes")


@pytest.mark.asyncio
async def test_a_bucket_that_never_existed_lists_nothing(admin_client, state_dir) -> None:
    response = await admin_client.get(_list_url("nosuchbucket"))

    assert response.status_code == 200
    assert response.json() == {"bucket": "nosuchbucket", "entries": [], "storage_bytes": 0}


@pytest.mark.asyncio
async def test_a_name_the_bucket_layer_refuses_is_rejected(admin_client, state_dir) -> None:
    response = await admin_client.get(_list_url("photos.bak"))

    assert response.status_code == 400
    assert response.json()["error"] == "HSYNC-1001"


# ── restore ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_restore_brings_a_deleted_file_back(admin_client, state_dir, bucket_dir) -> None:
    source = _write(bucket_dir / "docs" / "report.txt", b"first draft")
    entry_id = _capture(bucket_dir, "docs/report.txt", b"first draft")
    source.unlink()

    response = await admin_client.post(_restore_url(entry_id))

    assert response.status_code == 200
    assert response.json() == {
        "status": "restored",
        "path": "docs/report.txt",
        "size": len(b"first draft"),
    }
    assert source.read_bytes() == b"first draft"


@pytest.mark.asyncio
async def test_a_second_restore_of_the_same_bytes_changes_nothing(
    admin_client, state_dir, bucket_dir
) -> None:
    entry_id = _capture(bucket_dir, "a.txt", b"payload")
    (bucket_dir / "a.txt").unlink()

    first = await admin_client.post(_restore_url(entry_id))
    second = await admin_client.post(_restore_url(entry_id))

    assert first.json()["status"] == "restored"
    assert second.status_code == 200
    assert second.json()["status"] == "unchanged"


@pytest.mark.asyncio
async def test_an_occupied_path_needs_the_overwrite_flag(
    admin_client, state_dir, bucket_dir
) -> None:
    entry_id = _capture(bucket_dir, "a.txt", b"preserved")
    _replace(bucket_dir / "a.txt", b"current")

    refused = await admin_client.post(_restore_url(entry_id))

    assert refused.status_code == 409
    assert refused.json()["error"] == "HSYNC-3004"
    assert (bucket_dir / "a.txt").read_bytes() == b"current"

    accepted = await admin_client.post(_restore_url(entry_id), json={"overwrite": True})

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "restored"
    assert (bucket_dir / "a.txt").read_bytes() == b"preserved"
    # The bytes the overwrite replaced are preserved in turn, never discarded.
    relpaths = [item.entry["relpath"] for item in history_mod.entries(BUCKET)]
    assert relpaths == ["a.txt", "a.txt"]


@pytest.mark.asyncio
async def test_an_unknown_entry_is_not_found(admin_client, state_dir, bucket_dir) -> None:
    response = await admin_client.post(_restore_url("1700000000-deadbeef"))

    assert response.status_code == 404
    assert response.json()["error"] == "HSYNC-2000"


@pytest.mark.asyncio
async def test_restore_into_a_missing_bucket_is_not_found(
    admin_client, state_dir, bucket_dir
) -> None:
    entry_id = _capture(bucket_dir, "a.txt", b"kept")
    shutil.rmtree(bucket_dir)

    response = await admin_client.post(_restore_url(entry_id))

    assert response.status_code == 404
    assert response.json()["error"] == "HSYNC-2000"
    assert not bucket_dir.exists()


@pytest.mark.asyncio
async def test_a_damaged_copy_is_never_published(admin_client, state_dir, bucket_dir) -> None:
    entry_id = _capture(bucket_dir, "a.txt", b"0123456789")
    (bucket_dir / "a.txt").unlink()
    history_mod.entry(BUCKET, entry_id).payload.write_bytes(b"tampered!")

    response = await admin_client.post(_restore_url(entry_id))

    assert response.status_code == 409
    assert response.json()["error"] == "HSYNC-3004"
    assert not (bucket_dir / "a.txt").exists()


@pytest.mark.asyncio
async def test_a_failed_write_is_a_server_error(admin_client, state_dir, bucket_dir) -> None:
    entry_id = _capture(bucket_dir, "docs/report.txt", b"payload")
    (bucket_dir / "docs" / "report.txt").unlink()
    (bucket_dir / "docs").rmdir()
    _write(bucket_dir / "docs", b"a file where a directory must go")

    response = await admin_client.post(_restore_url(entry_id))

    assert response.status_code == 500
    assert response.json()["error"] == "HSYNC-4000"


# ── authorization ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_request_without_a_token_is_refused(state_dir, bucket_dir) -> None:
    _capture(bucket_dir, "a.txt")

    async with AsyncClient(
        transport=ASGITransport(app=_admin_app()), base_url="http://test"
    ) as client:
        response = await client.get(_list_url())

    # ``auth.verify_api_key`` answers a missing header itself — 401 plus the
    # Bearer challenge — so this refusal never reaches the router's envelope.
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "entries" not in response.json()


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused(state_dir, bucket_dir) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_admin_app()),
        base_url="http://test",
        headers={"Authorization": "Bearer " + "0" * 64},
    ) as client:
        response = await client.get(_list_url())

    assert response.status_code == 401
    assert response.json()["error"] in {"HSYNC-3000", "HSYNC-3001"}


@pytest.mark.asyncio
async def test_a_client_token_is_forbidden(state_dir, bucket_dir) -> None:
    from hsync_server.auth_store import get_auth_store

    _, token = get_auth_store().create_account("history-reader", "client")
    async with AsyncClient(
        transport=ASGITransport(app=_admin_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        listing = await client.get(_list_url())
        restore = await client.post(_restore_url("1700000000-deadbeef"))

    assert listing.status_code == 403
    assert restore.status_code == 403
    assert listing.json()["error"] == "HSYNC-3003"
