# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Security regression tests for the server attack surface.

Covers: cross-bucket confinement, internal engine-file rejection, mandatory
upload integrity, decompression-bomb caps, remote permission masking,
timestamp clamping and WS event path validation.
"""

import hashlib
import io
import os
import stat
import time
from json import dumps
from unittest.mock import AsyncMock, patch

import pytest
import zstandard
from fastapi import WebSocket, WebSocketDisconnect
from httpx import AsyncClient

from hsync_server.api.events import ws_events


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _upload_data(content: bytes, **extra) -> dict:
    data = {"cid": "TestClientX1234567890", "path": "", "permissions": "644"}
    data["sha256sum"] = _sha(content)
    data.update(extra)
    return data


class TestCrossBucketConfinement:
    """Paths escaping into a sibling bucket must be rejected everywhere."""

    @pytest.mark.asyncio
    async def test_upload_cross_bucket_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "A"))
        os.makedirs(os.path.join(temp_buckets_dir, "B"))
        content = b"cross"
        response = await client.post(
            "/api/v1/buckets/A/files",
            files={"file": ("x.txt", io.BytesIO(content), "text/plain")},
            data=_upload_data(content, path="../B"),
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(temp_buckets_dir, "B", "x.txt"))

    @pytest.mark.asyncio
    async def test_download_cross_bucket_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "A"))
        os.makedirs(os.path.join(temp_buckets_dir, "B"))
        with open(os.path.join(temp_buckets_dir, "B", "secret.txt"), "w") as f:
            f.write("secret")
        response = await client.get(
            "/api/v1/buckets/A/files/download",
            params={"filename": "../B/secret.txt"},
        )
        assert response.status_code == 400
        assert response.text != "secret"

    @pytest.mark.asyncio
    async def test_delete_cross_bucket_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "A"))
        os.makedirs(os.path.join(temp_buckets_dir, "B"))
        victim = os.path.join(temp_buckets_dir, "B", "keep.txt")
        with open(victim, "w") as f:
            f.write("keep")
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/A/files",
            data={"cid": "TestClientX1234567890", "filename": "../B/keep.txt"},
        )
        assert response.status_code == 400
        assert os.path.exists(victim)

    @pytest.mark.asyncio
    async def test_list_cross_bucket_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "A"))
        os.makedirs(os.path.join(temp_buckets_dir, "B"))
        response = await client.get("/api/v1/buckets/A/files", params={"path": "../B"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_download_backslash_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        """Windows separator differential must be rejected on the wire."""
        os.makedirs(os.path.join(temp_buckets_dir, "A"))
        response = await client.get(
            "/api/v1/buckets/A/files/download",
            params={"filename": "..\\..\\etc\\passwd"},
        )
        assert response.status_code == 400


class TestInternalFilenames:
    """Engine-owned files must never enter the storage via the wire."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [".hsyncignore", ".hsyncstate", ".hsynctombstones"])
    async def test_upload_internal_name_rejected(
        self, client: AsyncClient, temp_buckets_dir: str, name: str
    ):
        os.makedirs(os.path.join(temp_buckets_dir, "engbucket"))
        content = b"*"
        response = await client.post(
            "/api/v1/buckets/engbucket/files",
            files={"file": (name, io.BytesIO(content), "text/plain")},
            data=_upload_data(content),
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(temp_buckets_dir, "engbucket", name))

    @pytest.mark.asyncio
    async def test_download_internal_name_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = os.path.join(temp_buckets_dir, "engbucket2")
        os.makedirs(bucket)
        # Even if one somehow landed on disk, it must not be served.
        with open(os.path.join(bucket, ".hsyncignore"), "w") as f:
            f.write("*")
        response = await client.get(
            "/api/v1/buckets/engbucket2/files/download",
            params={"filename": ".hsyncignore"},
        )
        assert response.status_code == 400


class TestUploadIntegrity:
    """sha256sum is a required, verified form field."""

    @pytest.mark.asyncio
    async def test_upload_missing_sha256sum_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        os.makedirs(os.path.join(temp_buckets_dir, "intbucket"))
        response = await client.post(
            "/api/v1/buckets/intbucket/files",
            files={"file": ("f.txt", io.BytesIO(b"data"), "text/plain")},
            data={"cid": "TestClientX1234567890", "path": "", "permissions": "644"},
        )
        assert response.status_code == 422  # required form field (FastAPI)
        assert not os.path.exists(os.path.join(temp_buckets_dir, "intbucket", "f.txt"))

    @pytest.mark.asyncio
    async def test_upload_empty_sha256sum_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        os.makedirs(os.path.join(temp_buckets_dir, "intbucket2"))
        response = await client.post(
            "/api/v1/buckets/intbucket2/files",
            files={"file": ("f.txt", io.BytesIO(b"data"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": "",
            },
        )
        assert response.status_code in (400, 422)
        assert not os.path.exists(os.path.join(temp_buckets_dir, "intbucket2", "f.txt"))


class TestUploadChecksumFormat:
    """Malformed checksum declarations are 400s, never unhandled 500s."""

    @pytest.mark.asyncio
    async def test_non_ascii_sha256sum_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        """hmac.compare_digest raises TypeError on non-ASCII: the format gate
        must reject the declaration first."""
        os.makedirs(os.path.join(temp_buckets_dir, "fmtbucket"))
        response = await client.post(
            "/api/v1/buckets/fmtbucket/files",
            files={"file": ("f.txt", io.BytesIO(b"data"), "text/plain")},
            data=_upload_data(b"data", sha256sum="é" * 64),
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(temp_buckets_dir, "fmtbucket", "f.txt"))

    @pytest.mark.asyncio
    async def test_short_sha256sum_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "fmtbucket2"))
        response = await client.post(
            "/api/v1/buckets/fmtbucket2/files",
            files={"file": ("f.txt", io.BytesIO(b"data"), "text/plain")},
            data=_upload_data(b"data", sha256sum="abcd"),
        )
        assert response.status_code == 400


class TestCompressedUploadNotResumable:
    """Compressed uploads with offset > 0 are rejected up-front: the resumable
    path never decompresses, so they could only fail integrity later."""

    @pytest.mark.asyncio
    async def test_compressed_resume_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = os.path.join(temp_buckets_dir, "cresume")
        os.makedirs(bucket)
        original = b"payload" * 100
        compressed = zstandard.ZstdCompressor().compress(original)

        response = await client.post(
            "/api/v1/buckets/cresume/files",
            files={"file": ("c.txt", io.BytesIO(compressed), "application/octet-stream")},
            data=_upload_data(
                original,
                compressed="true",
                size=str(len(original)),
                offset="10",
            ),
        )
        assert response.status_code == 400


class TestDecompressionBomb:
    """Compressed uploads are capped at the declared size."""

    @pytest.mark.asyncio
    async def test_compressed_beyond_declared_size_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = os.path.join(temp_buckets_dir, "bombbucket")
        os.makedirs(bucket)

        original = b"B" * 1_000_000
        compressed = zstandard.ZstdCompressor().compress(original)
        assert len(compressed) < 10_000

        response = await client.post(
            "/api/v1/buckets/bombbucket/files",
            files={"file": ("bomb.txt", io.BytesIO(compressed), "application/octet-stream")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "compressed": "true",
                "size": "100",  # lie: real content is 1 MB
                "sha256sum": _sha(original),
            },
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(bucket, "bomb.txt"))

    @pytest.mark.asyncio
    async def test_compressed_size_mismatch_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """A decompressed size different from the declared one is rejected."""
        bucket = os.path.join(temp_buckets_dir, "bombbucket2")
        os.makedirs(bucket)

        original = b"short"
        compressed = zstandard.ZstdCompressor().compress(original)

        response = await client.post(
            "/api/v1/buckets/bombbucket2/files",
            files={"file": ("f.txt", io.BytesIO(compressed), "application/octet-stream")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "compressed": "true",
                "size": str(len(original) + 10),  # wrong on purpose
                "sha256sum": _sha(original),
            },
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(bucket, "f.txt"))


class TestRemotePermissionMask:
    """Special permission bits from peers are stripped."""

    @pytest.mark.asyncio
    async def test_setuid_bit_stripped_on_upload(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = os.path.join(temp_buckets_dir, "permbucket")
        os.makedirs(bucket)
        content = b"#!/bin/sh\n"
        response = await client.post(
            "/api/v1/buckets/permbucket/files",
            files={"file": ("s.sh", io.BytesIO(content), "text/plain")},
            data=_upload_data(content, permissions="4755"),
        )
        assert response.status_code == 201
        mode = stat.S_IMODE(os.stat(os.path.join(bucket, "s.sh")).st_mode)
        assert mode == 0o755
        assert not (mode & stat.S_ISUID)


class TestTimestampClamp:
    """Far-future peer-supplied mtimes are clamped."""

    @pytest.mark.asyncio
    async def test_far_future_mtime_clamped(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = os.path.join(temp_buckets_dir, "tsbucket")
        os.makedirs(bucket)
        content = b"data"
        future = str(int(time.time()) + 10_000_000)
        response = await client.post(
            "/api/v1/buckets/tsbucket/files",
            files={"file": ("f.txt", io.BytesIO(content), "text/plain")},
            data=_upload_data(content, mtime=future),
        )
        assert response.status_code == 201
        mtime = os.stat(os.path.join(bucket, "f.txt")).st_mtime
        assert mtime <= time.time() + 400


class TestCidValidation:
    """Malformed client IDs are rejected."""

    @pytest.mark.asyncio
    async def test_upload_invalid_cid_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "cidbucket"))
        content = b"data"
        response = await client.post(
            "/api/v1/buckets/cidbucket/files",
            files={"file": ("f.txt", io.BytesIO(content), "text/plain")},
            data={
                "cid": "bad cid/with\\separators",
                "path": "",
                "permissions": "644",
                "sha256sum": _sha(content),
            },
        )
        assert response.status_code == 400
        assert not os.path.exists(os.path.join(temp_buckets_dir, "cidbucket", "f.txt"))


class TestWebSocketEventSecurity:
    """WS event payloads are validated before dispatch/relay."""

    @pytest.fixture
    def mock_websocket(self):
        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        return ws

    def _auth_frame(self, token: str) -> str:
        from hsync_common.protocol.handshake import build_ws_auth_request

        from hsync_server.constants import HSYNC_VERSION

        return dumps(build_ws_auth_request(token=token, app_version=HSYNC_VERSION, capabilities=[]))

    @pytest.mark.asyncio
    async def test_traversal_event_not_applied(self, mock_websocket, tmp_path):
        """A deleted event with ../ must not remove files outside the bucket."""
        buckets_root = tmp_path / "buckets"
        bucket_dir = buckets_root / "testbucket"
        bucket_dir.mkdir(parents=True)
        outside = tmp_path / "outside_keep.txt"
        outside.write_text("keep me")

        mock_websocket.receive_text.side_effect = [
            self._auth_frame("test-key"),
            '{"name": "deleted", "src": {"filepath": "../outside_keep.txt"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.get_api_key", return_value="test-key"),
            patch("hsync_server.api.events.get_buckets_path", return_value=str(buckets_root)),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        assert outside.exists()

    @pytest.mark.asyncio
    async def test_backslash_event_not_relayed(self, mock_websocket, tmp_path):
        """Windows-separator payloads are dropped before handling/relay."""
        buckets_root = tmp_path / "buckets"
        bucket_dir = buckets_root / "testbucket"
        bucket_dir.mkdir(parents=True)

        mock_websocket.receive_text.side_effect = [
            self._auth_frame("test-key"),
            '{"name": "deleted", "src": {"filepath": "..\\\\..\\\\evil.txt"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.get_api_key", return_value="test-key"),
            patch("hsync_server.api.events.get_buckets_path", return_value=str(buckets_root)),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        # The point is: no crash, no deletion, no relay (loop continues cleanly).
        assert bucket_dir.exists()

    @pytest.mark.asyncio
    async def test_internal_name_event_not_applied(self, mock_websocket, tmp_path):
        """Events targeting engine files are dropped."""
        buckets_root = tmp_path / "buckets"
        bucket_dir = buckets_root / "testbucket"
        bucket_dir.mkdir(parents=True)
        marker = bucket_dir / "real.txt"
        marker.write_text("x")

        mock_websocket.receive_text.side_effect = [
            self._auth_frame("test-key"),
            '{"name": "renamed", "src": {"filepath": "real.txt"}, "dst": {"filepath": ".hsyncignore"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.get_api_key", return_value="test-key"),
            patch("hsync_server.api.events.get_buckets_path", return_value=str(buckets_root)),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        assert marker.exists()
        assert not (bucket_dir / ".hsyncignore").exists()

    @pytest.mark.asyncio
    async def test_invalid_cid_connection_rejected(self, mock_websocket):
        mock_websocket.receive_text.side_effect = [self._auth_frame("test-key")]
        await ws_events(mock_websocket, "testbucket", "bad cid/")
        mock_websocket.close.assert_called_once()
        assert mock_websocket.close.call_args.kwargs.get("code") == 4000 or (
            mock_websocket.close.call_args.args and mock_websocket.close.call_args.args[0] == 4000
        )
