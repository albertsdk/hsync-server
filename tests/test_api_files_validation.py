"""
Integration tests for file operations API.
"""

import hashlib
import io
import os

import pytest
from httpx import AsyncClient


class TestFileUploadValidation:
    """Tests for file upload validation."""

    @pytest.mark.asyncio
    async def test_upload_invalid_bucket_name(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload to invalid bucket name returns 400."""
        response = await client.post(
            "/api/v1/buckets/invalid..bucket/files",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"content").hexdigest(),
            },
        )
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_upload_path_traversal_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload with path traversal is rejected."""
        bucket_path = os.path.join(temp_buckets_dir, "securitybucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/securitybucket/files",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "../../../tmp",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"content").hexdigest(),
            },
        )
        assert response.status_code == 400
        assert "HSYNC-1002" in response.text

    @pytest.mark.asyncio
    async def test_upload_invalid_permissions(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload with invalid permissions returns 400."""
        bucket_path = os.path.join(temp_buckets_dir, "permbucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/permbucket/files",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "invalid",
                "sha256sum": hashlib.sha256(b"content").hexdigest(),
            },
        )
        assert response.status_code == 400
        assert "HSYNC-1003" in response.text

    @pytest.mark.asyncio
    async def test_upload_file_already_exists(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload identical file returns 409."""
        import hashlib

        bucket_path = os.path.join(temp_buckets_dir, "dupbucket")
        os.makedirs(bucket_path)

        file_content = b"duplicate content"
        sha256sum = hashlib.sha256(file_content).hexdigest()

        # First upload
        response = await client.post(
            "/api/v1/buckets/dupbucket/files",
            files={"file": ("dup.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": sha256sum,
            },
        )
        assert response.status_code == 201

        # Second upload with same content
        response = await client.post(
            "/api/v1/buckets/dupbucket/files",
            files={"file": ("dup.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": sha256sum,
            },
        )
        assert response.status_code == 409
        assert "already present" in response.json()["info"]
        # The 409 must return a bucket-relative path, never the
        # absolute filesystem path of the stored file.
        returned_path = response.json().get("file", "")
        assert temp_buckets_dir not in returned_path
        assert returned_path.startswith("dupbucket/")

    @pytest.mark.asyncio
    async def test_upload_file_already_exists_with_path(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """409 on a nested duplicate returns a bucket-relative path."""
        import hashlib

        bucket_path = os.path.join(temp_buckets_dir, "dupbucket2", "sub")
        os.makedirs(bucket_path)

        file_content = b"duplicate content"
        sha256sum = hashlib.sha256(file_content).hexdigest()

        response = await client.post(
            "/api/v1/buckets/dupbucket2/files",
            files={"file": ("dup.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "sub",
                "permissions": "644",
                "sha256sum": sha256sum,
            },
        )
        assert response.status_code == 201

        response = await client.post(
            "/api/v1/buckets/dupbucket2/files",
            files={"file": ("dup.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "sub",
                "permissions": "644",
                "sha256sum": sha256sum,
            },
        )
        assert response.status_code == 409
        returned_path = response.json().get("file", "")
        assert temp_buckets_dir not in returned_path
        assert "/" in returned_path

    @pytest.mark.asyncio
    async def test_upload_checksum_mismatch(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload with wrong checksum returns 400."""
        bucket_path = os.path.join(temp_buckets_dir, "checksumbucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/checksumbucket/files",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                # Well-formed but wrong digest (a malformed one is rejected
                # earlier by the format gate).
                "sha256sum": "0" * 64,
            },
        )
        assert response.status_code == 400
        assert "HSYNC-1005" in response.text


class TestFileSizeLimit:
    """Tests for file upload size limit."""

    @pytest.mark.asyncio
    async def test_upload_exceeds_size_limit(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload exceeding size limit returns 400."""
        from unittest.mock import patch

        bucket_path = os.path.join(temp_buckets_dir, "sizelimitbucket")
        os.makedirs(bucket_path)

        large_content = b"x" * 1000

        with patch("hsync_server.api.files.upload.get_max_upload_size", return_value=100):
            response = await client.post(
                "/api/v1/buckets/sizelimitbucket/files",
                files={"file": ("large.txt", io.BytesIO(large_content), "text/plain")},
                data={
                    "cid": "TestClientX1234567890",
                    "path": "",
                    "permissions": "644",
                    "sha256sum": hashlib.sha256(large_content).hexdigest(),
                },
            )

        assert response.status_code == 400
        assert "HSYNC-1006" in response.text

    @pytest.mark.asyncio
    async def test_upload_within_size_limit(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload within size limit succeeds."""
        from unittest.mock import patch

        bucket_path = os.path.join(temp_buckets_dir, "sizelimitbucket2")
        os.makedirs(bucket_path)

        small_content = b"small"

        with patch("hsync_server.api.files.upload.get_max_upload_size", return_value=1000):
            response = await client.post(
                "/api/v1/buckets/sizelimitbucket2/files",
                files={"file": ("small.txt", io.BytesIO(small_content), "text/plain")},
                data={
                    "cid": "TestClientX1234567890",
                    "path": "",
                    "permissions": "644",
                    "sha256sum": hashlib.sha256(small_content).hexdigest(),
                },
            )

        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_upload_size_limit_disabled(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload with size limit disabled (0) succeeds."""
        from unittest.mock import patch

        bucket_path = os.path.join(temp_buckets_dir, "nolimitbucket")
        os.makedirs(bucket_path)

        content = b"x" * 1000

        with patch("hsync_server.api.files.upload.get_max_upload_size", return_value=0):
            response = await client.post(
                "/api/v1/buckets/nolimitbucket/files",
                files={"file": ("file.txt", io.BytesIO(content), "text/plain")},
                data={
                    "cid": "TestClientX1234567890",
                    "path": "",
                    "permissions": "644",
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
            )

        assert response.status_code == 201


class TestFileDiskSpace:
    """Tests for the size-aware disk space guard on upload."""

    @pytest.mark.asyncio
    async def test_upload_rejected_when_size_exceeds_free_minus_reserve(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Free space above the reserve but below reserve+size must be rejected."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        bucket_path = os.path.join(temp_buckets_dir, "diskbucket")
        os.makedirs(bucket_path)

        content = b"x" * 1000
        reserve = 100  # bytes of reserve
        # 500 free bytes: above the reserve (100) but below reserve + size (1100)
        fake_usage = MagicMock(free=500)

        with (
            patch(
                "hsync_server.api.utils.get_settings",
                return_value=SimpleNamespace(min_free_disk_space=reserve),
            ),
            patch("hsync_common.disk.disk_usage", return_value=fake_usage),
        ):
            response = await client.post(
                "/api/v1/buckets/diskbucket/files",
                files={"file": ("disk.txt", io.BytesIO(content), "text/plain")},
                data={
                    "cid": "TestClientX1234567890",
                    "path": "",
                    "permissions": "644",
                    "size": str(len(content)),
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
            )

        # The public contract for out-of-space is HTTP 507 (InsufficientStorage),
        # not just "not 201": a 500/400 regression must fail here.
        assert response.status_code == 507
        assert "HSYNC-4004" in response.text

    @pytest.mark.asyncio
    async def test_upload_allowed_when_free_covers_reserve_plus_size(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Free space covering reserve + size must be accepted."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        bucket_path = os.path.join(temp_buckets_dir, "diskbucket2")
        os.makedirs(bucket_path)

        content = b"x" * 1000
        reserve = 100
        fake_usage = MagicMock(free=10_000)  # plenty of room

        with (
            patch(
                "hsync_server.api.utils.get_settings",
                return_value=SimpleNamespace(min_free_disk_space=reserve),
            ),
            patch("hsync_common.disk.disk_usage", return_value=fake_usage),
        ):
            response = await client.post(
                "/api/v1/buckets/diskbucket2/files",
                files={"file": ("disk.txt", io.BytesIO(content), "text/plain")},
                data={
                    "cid": "TestClientX1234567890",
                    "path": "",
                    "permissions": "644",
                    "size": str(len(content)),
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
            )

        assert response.status_code == 201


class TestFileDownloadValidation:
    """Tests for file download validation."""

    @pytest.mark.asyncio
    async def test_download_invalid_bucket_name(self, client: AsyncClient):
        """Download from invalid bucket name returns 400."""
        response = await client.get(
            "/api/v1/buckets/invalid..bucket/files/download", params={"filename": "test.txt"}
        )
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_download_invalid_filename(self, client: AsyncClient, temp_buckets_dir: str):
        """Download with path traversal filename returns 400."""
        bucket_path = os.path.join(temp_buckets_dir, "downloadbucket")
        os.makedirs(bucket_path)

        response = await client.get(
            "/api/v1/buckets/downloadbucket/files/download",
            params={"filename": "../../../etc/passwd"},
        )
        assert response.status_code == 400
        assert "HSYNC-1002" in response.text


class TestFileListValidation:
    """Tests for file list validation."""

    @pytest.mark.asyncio
    async def test_list_files_invalid_bucket_name(self, client: AsyncClient):
        """List files with invalid bucket name returns 400."""
        response = await client.get("/api/v1/buckets/invalid..bucket/files")
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_list_files_invalid_path(self, client: AsyncClient, temp_buckets_dir: str):
        """List files with path traversal returns 400."""
        bucket_path = os.path.join(temp_buckets_dir, "listbucket")
        os.makedirs(bucket_path)

        response = await client.get(
            "/api/v1/buckets/listbucket/files", params={"path": "../../../etc"}
        )
        assert response.status_code == 400
        assert "HSYNC-1002" in response.text

    @pytest.mark.asyncio
    async def test_list_files_bucket_not_found(self, client: AsyncClient):
        """List files from non-existent bucket returns 404."""
        response = await client.get("/api/v1/buckets/nonexistentbucket/files")
        assert response.status_code == 404
        assert "HSYNC-2001" in response.text

    @pytest.mark.asyncio
    async def test_list_files_skips_os_cache(self, client: AsyncClient, temp_buckets_dir: str):
        """List files skips OS cache files (.DS_Store, Thumbs.db)."""
        bucket_path = os.path.join(temp_buckets_dir, "cachebucket")
        os.makedirs(bucket_path)

        with open(os.path.join(bucket_path, "visible.txt"), "w") as f:
            f.write("visible")
        with open(os.path.join(bucket_path, ".DS_Store"), "w") as f:
            f.write("cache")
        with open(os.path.join(bucket_path, "Thumbs.db"), "w") as f:
            f.write("cache")

        response = await client.get("/api/v1/buckets/cachebucket/files")
        assert response.status_code == 200
        data = response.json()
        # Response is a list of file objects
        filenames = [f["filepath"] for f in data]
        assert any("visible.txt" in f for f in filenames)
        assert not any(".DS_Store" in f for f in filenames)
        assert not any("Thumbs.db" in f for f in filenames)

    @pytest.mark.asyncio
    async def test_list_files_includes_dotfiles(self, client: AsyncClient, temp_buckets_dir: str):
        """List files includes dotfiles (.env, .gitignore) - industry standard."""
        bucket_path = os.path.join(temp_buckets_dir, "dotfilebucket")
        os.makedirs(bucket_path)

        with open(os.path.join(bucket_path, "visible.txt"), "w") as f:
            f.write("visible")
        with open(os.path.join(bucket_path, ".env"), "w") as f:
            f.write("env")
        with open(os.path.join(bucket_path, ".gitignore"), "w") as f:
            f.write("git")

        response = await client.get("/api/v1/buckets/dotfilebucket/files")
        assert response.status_code == 200
        data = response.json()
        filenames = [f["filepath"] for f in data]
        assert any("visible.txt" in f for f in filenames)
        assert any(".env" in f for f in filenames)
        assert any(".gitignore" in f for f in filenames)


class TestSymlinkSafety:
    """Symlinks inside a bucket must never dereference out-of-bucket targets.

    ``/files`` (list) and ``/files/download`` must never follow a symlink whose
    target escapes the bucket: the listing must not expose the target's
    metadata/hash and the download must not serve its content.
    """

    @pytest.mark.asyncio
    async def test_list_skips_escaping_symlink(self, client: AsyncClient, temp_buckets_dir: str):
        """A file-symlink whose target escapes the bucket is not listed."""
        bucket_path = os.path.join(temp_buckets_dir, "symlinkbucket")
        os.makedirs(bucket_path)

        visible = os.path.join(bucket_path, "visible.txt")
        with open(visible, "w") as f:
            f.write("visible")

        outside = os.path.join(temp_buckets_dir, "outside_secret.txt")
        with open(outside, "w") as f:
            f.write("top-secret")
        os.symlink(outside, os.path.join(bucket_path, "secret"))

        response = await client.get("/api/v1/buckets/symlinkbucket/files")
        assert response.status_code == 200
        data = response.json()
        filenames = [f["filepath"] for f in data]
        assert any("visible.txt" in f for f in filenames)
        assert not any("secret" in f for f in filenames)
        # The escaping target's content hash must never leak through the listing.
        assert not any(
            f.get("sha256sum") == hashlib.sha256(b"top-secret").hexdigest() for f in data
        )

    @pytest.mark.asyncio
    async def test_list_keeps_in_bucket_symlink(self, client: AsyncClient, temp_buckets_dir: str):
        """A symlink whose target stays inside the bucket is still listed."""
        bucket_path = os.path.join(temp_buckets_dir, "symlinkinbucket")
        os.makedirs(bucket_path)

        real = os.path.join(bucket_path, "real.txt")
        with open(real, "w") as f:
            f.write("inside")
        os.symlink("real.txt", os.path.join(bucket_path, "alias"))

        response = await client.get("/api/v1/buckets/symlinkinbucket/files")
        assert response.status_code == 200
        data = response.json()
        filenames = [f["filepath"] for f in data]
        assert any("alias" in f for f in filenames)

    @pytest.mark.asyncio
    async def test_download_refuses_escaping_symlink(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Downloading an escaping file-symlink never serves the target content.

        The full-resolve confinement rejects the path (400) because the target
        is outside the bucket — the outside bytes are never leaked."""
        bucket_path = os.path.join(temp_buckets_dir, "symlinkdlbucket")
        os.makedirs(bucket_path)

        outside = os.path.join(temp_buckets_dir, "outside_dl_secret.txt")
        with open(outside, "w") as f:
            f.write("top-secret-content")
        os.symlink(outside, os.path.join(bucket_path, "secret"))

        response = await client.get(
            "/api/v1/buckets/symlinkdlbucket/files/download", params={"filename": "secret"}
        )
        assert response.status_code == 400
        assert b"top-secret-content" not in response.content

    @pytest.mark.asyncio
    async def test_download_serves_in_bucket_symlink_target(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """A symlink whose target stays inside the bucket is served
        transparently (static-server semantics): this endpoint serves content
        on demand, while replication happens structurally via WS events and
        POST /symlinks. Symlinks are NOT refused wholesale."""
        bucket_path = os.path.join(temp_buckets_dir, "symlinkdlin")
        os.makedirs(bucket_path)

        real = os.path.join(bucket_path, "real.txt")
        with open(real, "w") as f:
            f.write("inside-content")
        os.symlink("real.txt", os.path.join(bucket_path, "alias"))

        response = await client.get(
            "/api/v1/buckets/symlinkdlin/files/download", params={"filename": "alias"}
        )
        assert response.status_code == 200
        assert response.content == b"inside-content"
        # The link must not be touched: it is still a symlink on disk.
        assert os.path.islink(os.path.join(bucket_path, "alias"))


class TestFileDeleteValidation:
    """Tests for file delete validation."""

    @pytest.mark.asyncio
    async def test_delete_invalid_bucket_name(self, client: AsyncClient):
        """Delete from invalid bucket name returns 400."""
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/invalid..bucket/files",
            data={"cid": "TestClientX1234567890", "filename": "test.txt"},
        )
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_delete_bucket_not_found(self, client: AsyncClient):
        """Delete from non-existent bucket returns 404."""
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/nonexistentbucket/files",
            data={"cid": "TestClientX1234567890", "filename": "test.txt"},
        )
        assert response.status_code == 404
        assert "HSYNC-2001" in response.text

    @pytest.mark.asyncio
    async def test_delete_path_traversal(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete with path traversal returns 400."""
        bucket_path = os.path.join(temp_buckets_dir, "deletebucket3")
        os.makedirs(bucket_path)

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/deletebucket3/files",
            data={"cid": "TestClientX1234567890", "filename": "../../../etc/passwd"},
        )
        assert response.status_code == 400
        assert "HSYNC-1002" in response.text

    @pytest.mark.asyncio
    async def test_delete_oserror(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete with OSError returns 500."""
        from unittest.mock import patch

        bucket_path = os.path.join(temp_buckets_dir, "deletebucket4")
        os.makedirs(bucket_path)
        file_path = os.path.join(bucket_path, "test.txt")
        with open(file_path, "w") as f:
            f.write("content")

        with patch("os.remove", side_effect=OSError("Permission denied")):
            response = await client.request(
                "DELETE",
                "/api/v1/buckets/deletebucket4/files",
                data={"cid": "TestClientX1234567890", "filename": "test.txt"},
            )

        assert response.status_code == 500
        assert "HSYNC-4003" in response.text
