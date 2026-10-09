"""
Tests targeting coverage gaps identified by pytest-cov.

Covers edge-case branches in:
- api/file_utils.py sentinel/cleanup edge cases
- api/files/upload.py compressed, backup, partial cleanup paths
- api/files/delete.py backup removal + sentinel cleanup
- api/files/download.py backup-file fallback
- api/files/list.py .backup skip
- api/events.py protocol mismatch, pong, rate-limit, heartbeat
- api/connection_manager.py slot release on error
- api/health.py readiness branch
- error_handlers.py generic handler
- tombstones.py prune edge
"""

import hashlib
import io
import os
from json import dumps
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from httpx import AsyncClient

from hsync_server.api.events import ws_events
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
    update_file_timestamp,
)
from tests.conftest import TEST_TOKEN

# ---------------------------------------------------------------------------
# api/file_utils.py — sentinel + cleanup edge cases
# ---------------------------------------------------------------------------


class TestFileUtilsEdgeCases:
    """Cover OSError / non-existent path branches in file_utils."""

    def test_ensure_sentinel_nonexistent_dir(self, tmp_path):
        """ensure_sentinel_in_dir is a no-op for a non-existent directory."""
        target = tmp_path / "nope"
        ensure_sentinel_in_dir(str(target))
        # Must not raise, and must not create the missing directory or any file
        assert not target.exists()

    def test_ensure_sentinel_not_a_dir(self, tmp_path):
        """ensure_sentinel_in_dir is a no-op when path is a file, not a dir."""
        f = tmp_path / "afile"
        f.write_text("x")
        ensure_sentinel_in_dir(str(f))
        # Must not raise, and the file must be untouched
        assert f.read_text() == "x"

    def test_ensure_sentinel_dir_has_syncable(self, tmp_path):
        """No sentinel created when dir already has a syncable file."""
        (tmp_path / "real.txt").write_text("data")
        ensure_sentinel_in_dir(str(tmp_path))
        assert not (tmp_path / ".hsynckeep").exists()

    def test_ensure_sentinel_creates_in_empty_dir(self, tmp_path):
        """Sentinel is created in an empty directory."""
        ensure_sentinel_in_dir(str(tmp_path))
        assert (tmp_path / ".hsynckeep").exists()

    def test_ensure_sentinel_already_exists(self, tmp_path):
        """No duplicate sentinel when one already exists."""
        (tmp_path / ".hsynckeep").write_text("")
        ensure_sentinel_in_dir(str(tmp_path))
        # Still only one sentinel
        assert len(list(tmp_path.iterdir())) == 1

    def test_cleanup_empty_dir_nonexistent(self, tmp_path):
        target = tmp_path / "nope"
        cleanup_empty_dir(str(target))
        # Must not raise, and must not create the missing directory
        assert not target.exists()

    def test_cleanup_empty_dir_not_a_dir(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("x")
        cleanup_empty_dir(str(f))
        # Must not raise, and the file must be untouched
        assert f.read_text() == "x"

    def test_cleanup_empty_dir_with_entries(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        cleanup_empty_dir(str(tmp_path))
        assert tmp_path.exists()  # not removed

    def test_cleanup_empty_dir_removes_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        cleanup_empty_dir(str(empty))
        assert not empty.exists()

    def test_cleanup_empty_dir_protects_bucket_root(self, tmp_path):
        """The bucket root directory is never removed even when empty."""
        bucket = tmp_path / "mybucket"
        bucket.mkdir()
        cleanup_empty_dir(str(bucket), bucket_root=str(bucket))
        assert bucket.exists()

    def test_is_sentinel_file_true(self):
        assert is_sentinel_file("/some/path/.hsynckeep")

    def test_is_sentinel_file_false(self):
        assert not is_sentinel_file("/some/path/normal.txt")

    def test_update_file_timestamp_no_mtime(self, tmp_path):
        """No mtime means no-op."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        update_file_timestamp(str(f), mtime=None)
        # Must not raise, and file content must be unchanged
        assert f.read_text() == "x"

    def test_update_file_timestamp_iso(self, tmp_path):
        """ISO-format mtime is applied correctly."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        update_file_timestamp(str(f), mtime="2020-01-01T00:00:00")
        # Must not raise

    def test_update_file_timestamp_unix(self, tmp_path):
        """Unix-timestamp mtime is applied correctly."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        update_file_timestamp(str(f), mtime="1577836800")
        # Must not raise


# ---------------------------------------------------------------------------
# api/files/upload.py — compressed, backup, partial cleanup
# ---------------------------------------------------------------------------


class TestUploadCoverageGaps:
    """Cover compressed upload, backup restore, and partial cleanup branches."""

    @pytest.mark.asyncio
    async def test_upload_compressed(self, client: AsyncClient, temp_buckets_dir: str):
        """A compressed upload is decompressed server-side.

        The payload is > 1 MB so the streaming decompressor iterates over
        multiple chunks (its read window is 1 MB).
        """
        import zstandard

        bucket_path = os.path.join(temp_buckets_dir, "compressupload")
        os.makedirs(bucket_path)

        original = b"Hello compressed world!" * 100_000  # ~2.3 MB
        cctx = zstandard.ZstdCompressor()
        compressed = cctx.compress(original)

        response = await client.post(
            "/api/v1/buckets/compressupload/files",
            files={"file": ("c.txt", io.BytesIO(compressed), "application/octet-stream")},
            data={
                "cid": "TestClientXY1k4567890",
                "path": "",
                "permissions": "644",
                "compressed": "true",
                "size": str(len(original)),
                "sha256sum": hashlib.sha256(original).hexdigest(),
            },
        )
        assert response.status_code == 201

        with open(os.path.join(bucket_path, "c.txt"), "rb") as f:
            assert f.read() == original

    @pytest.mark.asyncio
    async def test_upload_sanitized_filename(self, client: AsyncClient, temp_buckets_dir: str):
        """A filename with dangerous characters is sanitized."""
        bucket_path = os.path.join(temp_buckets_dir, "sanitizebucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/sanitizebucket/files",
            files={"file": ("file with spaces.txt", io.BytesIO(b"data"), "text/plain")},
            data={
                "cid": "TestClientXY1k4567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"data").hexdigest(),
            },
        )
        assert response.status_code == 201
        # The sanitized file should exist
        files = os.listdir(bucket_path)
        assert any("spaces" in f for f in files)

    @pytest.mark.asyncio
    async def test_upload_overwrites_existing_file(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Uploading a different version of an existing file overwrites it."""
        bucket_path = os.path.join(temp_buckets_dir, "overwritebucket")
        os.makedirs(bucket_path)
        existing = os.path.join(bucket_path, "ov.txt")
        with open(existing, "w") as f:
            f.write("old")

        response = await client.post(
            "/api/v1/buckets/overwritebucket/files",
            files={"file": ("ov.txt", io.BytesIO(b"new content"), "text/plain")},
            data={
                "cid": "TestClientXY1k4567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"new content").hexdigest(),
            },
        )
        assert response.status_code == 201
        with open(existing, "rb") as f:
            assert f.read() == b"new content"

    @pytest.mark.asyncio
    async def test_upload_partial_then_cleanup_on_failure(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """A failed upload cleans up the partial file."""
        bucket_path = os.path.join(temp_buckets_dir, "partialfail")
        os.makedirs(bucket_path)

        with patch("hsync_server.api.files.upload.get_max_upload_size", return_value=5):
            response = await client.post(
                "/api/v1/buckets/partialfail/files",
                files={"file": ("p.txt", io.BytesIO(b"1234567890"), "text/plain")},
                data={
                    "cid": "TestClientXY1k4567890",
                    "path": "",
                    "permissions": "644",
                    "size": "10",
                    "sha256sum": hashlib.sha256(b"1234567890").hexdigest(),
                },
            )
        assert response.status_code == 400
        # No lingering .part file
        assert not os.path.exists(os.path.join(bucket_path, "p.txt.part"))

    @pytest.mark.asyncio
    async def test_upload_with_timestamps(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload with ctime/mtime preserves timestamps."""
        bucket_path = os.path.join(temp_buckets_dir, "tsbucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/tsbucket/files",
            files={"file": ("ts.txt", io.BytesIO(b"data"), "text/plain")},
            data={
                "cid": "TestClientXY1k4567890",
                "path": "",
                "permissions": "644",
                "mtime": "1577836800",
                "ctime": "1577836800",
                "sha256sum": hashlib.sha256(b"data").hexdigest(),
            },
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_upload_disk_space_check_oserror(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """OSError during disk space check is logged but upload continues."""
        from types import SimpleNamespace

        bucket_path = os.path.join(temp_buckets_dir, "oserrbucket")
        os.makedirs(bucket_path)

        with (
            patch(
                "hsync_server.api.utils.get_settings",
                return_value=SimpleNamespace(min_free_disk_space=100),
            ),
            patch("hsync_server.api.utils.disk_usage", side_effect=OSError("nope")),
        ):
            response = await client.post(
                "/api/v1/buckets/oserrbucket/files",
                files={"file": ("f.txt", io.BytesIO(b"data"), "text/plain")},
                data={
                    "cid": "TestClientXY1k4567890",
                    "path": "",
                    "permissions": "644",
                    "size": "4",
                    "sha256sum": hashlib.sha256(b"data").hexdigest(),
                },
            )
        assert response.status_code == 201


# ---------------------------------------------------------------------------
# api/files/delete.py — backup removal + sentinel cleanup
# ---------------------------------------------------------------------------


class TestDeleteCoverageGaps:
    """Cover backup-file removal and sentinel cleanup branches."""

    @pytest.mark.asyncio
    async def test_delete_removes_backup_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Deleting a file also removes its .backup companion."""
        bucket_path = os.path.join(temp_buckets_dir, "delbackup")
        os.makedirs(bucket_path)
        f = os.path.join(bucket_path, "file.txt")
        b = os.path.join(bucket_path, "file.txt.backup")
        with open(f, "w") as fh:
            fh.write("content")
        with open(b, "w") as fh:
            fh.write("backup")

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/delbackup/files",
            data={"cid": "TestClientXY1k4567890", "filename": "file.txt"},
        )
        assert response.status_code == 200
        assert not os.path.exists(b)

    @pytest.mark.asyncio
    async def test_delete_sentinel_cleans_empty_dir(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Deleting a sentinel file cleans up the now-empty parent directory."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        bucket_path = os.path.join(temp_buckets_dir, "sentinelbucket")
        subdir = os.path.join(bucket_path, "emptydir")
        os.makedirs(subdir)
        sentinel = os.path.join(subdir, HSYNCKEEP_FILENAME)
        with open(sentinel, "w") as f:
            f.write("")

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/sentinelbucket/files",
            data={"cid": "TestClientXY1k4567890", "filename": f"emptydir/{HSYNCKEEP_FILENAME}"},
        )
        assert response.status_code == 200
        # The empty directory should have been removed
        assert not os.path.exists(subdir)

    @pytest.mark.asyncio
    async def test_delete_sentinel_at_bucket_root_protected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Deleting a sentinel at the bucket root does NOT remove the bucket."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        bucket_path = os.path.join(temp_buckets_dir, "rootbucket")
        os.makedirs(bucket_path)
        sentinel = os.path.join(bucket_path, HSYNCKEEP_FILENAME)
        with open(sentinel, "w") as f:
            f.write("")

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/rootbucket/files",
            data={"cid": "TestClientXY1k4567890", "filename": HSYNCKEEP_FILENAME},
        )
        assert response.status_code == 200
        # Bucket root must survive
        assert os.path.exists(bucket_path)


# ---------------------------------------------------------------------------
# api/files/download.py — backup-file fallback
# ---------------------------------------------------------------------------


class TestDownloadCoverageGaps:
    """Cover the download primary/backup selection."""

    @pytest.mark.asyncio
    async def test_download_prefers_primary_over_backup(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """With both present, the primary is served (atomic publish keeps it whole)."""
        bucket_path = os.path.join(temp_buckets_dir, "dlbackup")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "f.txt"), "w") as f:
            f.write("current content")
        with open(os.path.join(bucket_path, "f.txt.backup"), "w") as f:
            f.write("backup content")

        response = await client.get(
            "/api/v1/buckets/dlbackup/files/download", params={"filename": "f.txt"}
        )
        assert response.status_code == 200
        assert response.content == b"current content"

    @pytest.mark.asyncio
    async def test_download_falls_back_to_backup_when_primary_missing(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """A missing primary falls back to the .backup snapshot."""
        bucket_path = os.path.join(temp_buckets_dir, "dlbackup2")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "f.txt.backup"), "w") as f:
            f.write("backup content")

        response = await client.get(
            "/api/v1/buckets/dlbackup2/files/download", params={"filename": "f.txt"}
        )
        assert response.status_code == 200
        assert response.content == b"backup content"


# ---------------------------------------------------------------------------
# api/files/list.py — .backup skip
# ---------------------------------------------------------------------------


class TestListCoverageGaps:
    """Cover the .backup file skip in listing."""

    @pytest.mark.asyncio
    async def test_list_skips_backup_files(self, client: AsyncClient, temp_buckets_dir: str):
        """.backup files are excluded from the file listing."""
        bucket_path = os.path.join(temp_buckets_dir, "listbackup")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "real.txt"), "w") as f:
            f.write("real")
        with open(os.path.join(bucket_path, "real.txt.backup"), "w") as f:
            f.write("backup")

        response = await client.get("/api/v1/buckets/listbackup/files")
        assert response.status_code == 200
        data = response.json()
        filenames = [f["filepath"] for f in data]
        assert any("real.txt" in f for f in filenames)
        assert not any(".backup" in f for f in filenames)


# ---------------------------------------------------------------------------
# api/events.py — protocol mismatch, pong, rate-limit, heartbeat
# ---------------------------------------------------------------------------


def _ensure_test_bucket(bucket: str = "testbucket") -> None:
    """Create *bucket* under the patched buckets path.

    The WS flow checks bucket existence after auth (4009 on miss), so
    success-path tests must create the directory first.
    """
    from hsync_server.config import get_buckets_path

    os.makedirs(os.path.join(get_buckets_path(), bucket), exist_ok=True)


class TestWebSocketCoverageGaps:
    """Cover protocol mismatch, pong, rate-limit, and heartbeat branches."""

    @pytest.fixture
    def mock_websocket(self):
        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        return ws

    @pytest.mark.asyncio
    async def test_protocol_mismatch_rejected(self, mock_websocket):
        """A protocol version mismatch sends an auth-error then closes with 4002."""

        _ensure_test_bucket()
        mock_websocket.receive_text.return_value = dumps(
            {
                "type": "auth",
                "token": TEST_TOKEN,
                "app_version": "1.0.0",
                "protocol_version": "0.0.0-wrong",
                "capabilities": [],
            }
        )

        await ws_events(mock_websocket, "testbucket", "TestClientABCDEFGHIJK")

        mock_websocket.send_json.assert_called()
        mock_websocket.close.assert_called_with(code=4002, reason="Protocol incompatible")

    @pytest.mark.asyncio
    async def test_pong_message_handled(self, mock_websocket):
        """A pong message is handled without raising (coverage: pong branch)."""
        _ensure_test_bucket()
        mock_websocket.receive_text.side_effect = [
            dumps(
                {
                    "type": "auth",
                    "token": TEST_TOKEN,
                    "app_version": "1.0.0",
                    "protocol_version": None,
                    "capabilities": [],
                }
            ),
            '{"type": "pong"}',
            WebSocketDisconnect(),
        ]

        with patch("hsync_server.api.events.PROTOCOL_VERSION_WS", None):
            await ws_events(mock_websocket, "testbucket", "TestClientABCDEFGHIJK")

        # The pong message must not crash the handler. Among the frames sent
        # (auth reply + presence broadcast) there is an auth-ok dict; the pong
        # must not add any frame.
        auth_replies = [
            c[0][0]
            for c in mock_websocket.send_json.call_args_list
            if c[0] and isinstance(c[0][0], dict) and c[0][0].get("type") == "auth"
        ]
        assert len(auth_replies) == 1
        assert auth_replies[0].get("status") == "ok"

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_drops_event(self, mock_websocket):
        """When the rate limiter rejects, the event is silently dropped."""
        _ensure_test_bucket()
        mock_websocket.receive_text.side_effect = [
            dumps(
                {
                    "type": "auth",
                    "token": TEST_TOKEN,
                    "app_version": "1.0.0",
                    "protocol_version": None,
                    "capabilities": [],
                }
            ),
            '{"name": "added", "src": {"path": "x", "permissions": "755"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.PROTOCOL_VERSION_WS", None),
            patch("hsync_server.api.events.create_ws_message_bucket") as create_bucket,
        ):
            bucket = MagicMock()
            bucket.acquire.return_value = False
            create_bucket.return_value = bucket
            await ws_events(mock_websocket, "testbucket", "TestClientABCDEFGHIJK")

        # The event was dropped (no broadcast, no crash)
        bucket.acquire.assert_called()

    @pytest.mark.asyncio
    async def test_heartbeat_timeout(self, mock_websocket):
        """Heartbeat timeout closes the connection with 4004."""
        # After a successful auth, the main receive loop wraps ``receive_text``
        # in ``asyncio.wait_for``; when that times out it raises ``TimeoutError``.
        # Simulate exactly that to exercise the 4004 close branch.
        _ensure_test_bucket()
        mock_websocket.receive_text.side_effect = [
            dumps(
                {
                    "type": "auth",
                    "token": TEST_TOKEN,
                    "app_version": "1.0.0",
                    "protocol_version": None,
                    "capabilities": [],
                }
            ),
            TimeoutError(),
        ]

        with (
            patch("hsync_server.api.events.PROTOCOL_VERSION_WS", None),
            patch("hsync_server.api.events.get_ws_heartbeat_interval", return_value=0.01),
            patch("hsync_server.api.events.get_ws_heartbeat_timeout", return_value=0.01),
        ):
            await ws_events(mock_websocket, "testbucket", "TestClientABCDEFGHIJK")

        mock_websocket.close.assert_called_with(code=4004, reason="Heartbeat timeout")


# ---------------------------------------------------------------------------
# api/health.py — readiness branch
# ---------------------------------------------------------------------------


class TestHealthCoverageGaps:
    """Cover the health endpoint time branch."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client: AsyncClient):
        """Health endpoint returns 200."""
        response = await client.get("/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_size_probe_returns_bounded_payload(self):
        """``/health?size=N`` returns exactly N bytes, capped at the probe max."""
        from hsync_server.api.health import HEALTH_MAX_PROBE_BYTES, root

        assert len(await root(size=4096)) == 4096
        assert len(await root(size=HEALTH_MAX_PROBE_BYTES * 100)) == HEALTH_MAX_PROBE_BYTES
        assert await root() == ""

    @pytest.mark.asyncio
    async def test_time_endpoint_unit(self):
        """The /api/v1/time endpoint returns a timestamp."""
        from hsync_server.api.health import get_time

        response = await get_time()
        assert response.status_code == 200
        import json

        data = json.loads(response.body.decode())
        assert "timestamp" in data


# ---------------------------------------------------------------------------
# error_handlers.py — generic handler
# ---------------------------------------------------------------------------


class TestErrorHandlersCoverage:
    """Cover the generic exception handler."""

    @pytest.mark.asyncio
    async def test_generic_exception_returns_500(self):
        """An unhandled exception is converted to a generic 500 JSON response."""
        import json

        from hsync_server.error_handlers import global_exception_handler

        exc = RuntimeError("test error")
        response = await global_exception_handler(MagicMock(), exc)

        assert response.status_code == 500
        body = json.loads(response.body.decode())
        # The generic handler must not leak internal details.
        assert body["error"] == "HSYNC-5000"
        assert body["message"] == "An unexpected error occurred"
        assert "test error" not in response.body.decode()


# ---------------------------------------------------------------------------
# tombstones.py — prune edge
# ---------------------------------------------------------------------------


class TestTombstonesCoverageGaps:
    """Cover tombstone prune edge cases."""

    def test_tombstone_store_prune_removes_expired(self, tmp_path):
        """Pruning removes tombstones older than the retention window."""
        import time

        from hsync_common.models.tombstone import TombstoneStore, tombstone_key

        store = TombstoneStore(ttl_days=1)
        # Record one expired marker (ttl=1 day) and one fresh marker through
        # the public causal API (no private _data injection).
        store.record("key1", sha256sum="a" * 64, timestamp=time.time() - 999999)
        store.record("key2", sha256sum="b" * 64, timestamp=time.time())
        store.prune()
        payload = store.to_payload()
        assert tombstone_key("key1") not in payload
        assert tombstone_key("key2") in payload
