"""
Integration tests for file operations API.
"""

import hashlib
import io
import os

import pytest
from httpx import AsyncClient


class TestFileUpload:
    """Tests for file upload endpoint."""

    @pytest.mark.asyncio
    async def test_upload_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload a file to a bucket."""
        bucket_path = os.path.join(temp_buckets_dir, "uploadbucket")
        os.makedirs(bucket_path)

        # Upload file - cid is required
        file_content = b"Hello, World!"
        response = await client.post(
            "/api/v1/buckets/uploadbucket/files",
            files={"file": ("test.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(file_content).hexdigest(),
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["info"] == "File uploaded successfully"

        uploaded_file = os.path.join(bucket_path, "test.txt")
        assert os.path.exists(uploaded_file)
        with open(uploaded_file, "rb") as f:
            assert f.read() == file_content

    @pytest.mark.asyncio
    async def test_upload_file_with_path(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload a file to a subdirectory."""
        bucket_path = os.path.join(temp_buckets_dir, "uploadbucket2")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/uploadbucket2/files",
            files={"file": ("nested.txt", io.BytesIO(b"nested"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "subdir/deep",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"nested").hexdigest(),
            },
        )

        assert response.status_code == 201

        nested_file = os.path.join(bucket_path, "subdir", "deep", "nested.txt")
        assert os.path.exists(nested_file)

    @pytest.mark.asyncio
    async def test_upload_creates_directory(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload creates directory structure if needed."""
        # When uploading to a new bucket, the directory is created automatically
        response = await client.post(
            "/api/v1/buckets/newbucket/files",
            files={"file": ("test.txt", io.BytesIO(b"test"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "subdir",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"test").hexdigest(),
            },
        )

        # Upload succeeds and creates the directory structure
        assert response.status_code == 201

        expected_path = os.path.join(temp_buckets_dir, "newbucket", "subdir", "test.txt")
        assert os.path.exists(expected_path)


class TestFileDownload:
    """Tests for file download endpoint."""

    @pytest.mark.asyncio
    async def test_download_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Download a file from a bucket."""
        # Create bucket and file (use alphanumeric name)
        bucket_path = os.path.join(temp_buckets_dir, "downloadbucket")
        os.makedirs(bucket_path)
        file_path = os.path.join(bucket_path, "download.txt")
        with open(file_path, "w") as f:
            f.write("Download content")

        response = await client.get(
            "/api/v1/buckets/downloadbucket/files/download", params={"filename": "download.txt"}
        )

        assert response.status_code == 200
        assert response.content == b"Download content"

    @pytest.mark.asyncio
    async def test_download_nonexistent_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Download non-existent file returns 404."""
        bucket_path = os.path.join(temp_buckets_dir, "downloadbucket2")
        os.makedirs(bucket_path)

        response = await client.get(
            "/api/v1/buckets/downloadbucket2/files/download", params={"filename": "missing.txt"}
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_download_compressed_streaming(self, client: AsyncClient, temp_buckets_dir: str):
        """A large compressible file is streamed zstd-compressed and round-trips (2.11)."""
        from unittest.mock import patch

        bucket_path = os.path.join(temp_buckets_dir, "compressbucket")
        os.makedirs(bucket_path)
        file_path = os.path.join(bucket_path, "big.txt")
        # Compressible content above the 10KB download threshold.
        content = ("compressible streaming download line\n" * 5000).encode()
        with open(file_path, "wb") as f:
            f.write(content)

        with patch("hsync_server.api.files.download.get_compression_enabled", return_value=True):
            response = await client.get(
                "/api/v1/buckets/compressbucket/files/download",
                params={"filename": "big.txt"},
            )

        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "zstd"
        # httpx transparently decodes the zstd Content-Encoding, so reaching the
        # original bytes proves the streamed body is a single, valid zstd frame
        # decodable by a real client (one-shot decompression compatibility).
        assert response.content == content


class TestFileList:
    """Tests for file listing endpoint."""

    @pytest.mark.asyncio
    async def test_list_files(self, client: AsyncClient, temp_buckets_dir: str):
        """List files in a bucket."""
        # Create bucket with files (use alphanumeric name)
        bucket_path = os.path.join(temp_buckets_dir, "listbucket")
        os.makedirs(bucket_path)

        for i in range(3):
            with open(os.path.join(bucket_path, f"file{i}.txt"), "w") as f:
                f.write(f"content {i}")

        response = await client.get("/api/v1/buckets/listbucket/files")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_list_files_relative_buckets_config_yields_relpaths(
        self,
        client: AsyncClient,
        temp_buckets_dir: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A relative HSYNC_BUCKETS config must never leak absolute paths on the wire.

        Regression: ``get_files`` compared the (resolved, absolute) walk paths
        against a raw relative ``bucket_root``. ``File.from_dict``'s
        ``relative_to`` then failed and fell back to the verbatim ABSOLUTE
        path, which peers reject as an unsafe remote listing entry.

        Only the two modules consulted by ``get_files`` path math are switched
        to a relative config string (resolving to the very same buckets dir),
        exactly what the shipped ``./buckets`` default does inside the
        container.
        """
        from hsync_server.api import utils as utils_module
        from hsync_server.api.files import list as list_module

        bucket_path = os.path.join(temp_buckets_dir, "rellistbucket")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "file0.txt"), "w") as f:
            f.write("content")

        # Relative spelling of the same absolute buckets dir.
        rel_config = os.path.relpath(temp_buckets_dir, os.getcwd())
        monkeypatch.setattr(utils_module, "get_buckets_path", lambda: rel_config)
        monkeypatch.setattr(list_module, "get_buckets_path", lambda: rel_config)

        response = await client.get("/api/v1/buckets/rellistbucket/files")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        filepath = data[0]["filepath"]
        assert filepath == "file0.txt"
        assert not filepath.startswith("/")

    @pytest.mark.asyncio
    async def test_list_files_empty_bucket(self, client: AsyncClient, temp_buckets_dir: str):
        """List files in empty bucket returns empty list."""
        bucket_path = os.path.join(temp_buckets_dir, "emptybucket")
        os.makedirs(bucket_path)

        response = await client.get("/api/v1/buckets/emptybucket/files")

        assert response.status_code == 200
        assert response.json() == []


class TestFileDelete:
    """Tests for file deletion endpoint."""

    @pytest.mark.asyncio
    async def test_delete_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete a file from a bucket."""
        # Create bucket and file (use alphanumeric name)
        bucket_path = os.path.join(temp_buckets_dir, "deletebucket")
        os.makedirs(bucket_path)
        file_path = os.path.join(bucket_path, "todelete.txt")
        with open(file_path, "w") as f:
            f.write("delete me")

        # delete_file requires cid and filename as form data
        # httpx.delete() doesn't support data, use request() instead
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/deletebucket/files",
            data={"cid": "TestClientX1234567890", "filename": "todelete.txt"},
        )

        assert response.status_code == 200
        assert not os.path.exists(file_path)

        # Verify tombstone was recorded — a critical side-effect for sync consistency
        tombs_resp = await client.get("/api/v1/buckets/deletebucket/tombstones")
        assert tombs_resp.status_code == 200
        tombs = tombs_resp.json()
        assert len(tombs) == 1

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete non-existent file returns 404."""
        bucket_path = os.path.join(temp_buckets_dir, "deletebucket2")
        os.makedirs(bucket_path)

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/deletebucket2/files",
            data={"cid": "TestClientX1234567890", "filename": "missing.txt"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_failure_does_not_leak_filesystem_path(
        self, client: AsyncClient, temp_buckets_dir: str, monkeypatch
    ):
        """An OSError message (absolute path) stays server-side."""
        bucket_path = os.path.join(temp_buckets_dir, "deletebucket3")
        os.makedirs(bucket_path)
        file_path = os.path.join(bucket_path, "x.txt")
        with open(file_path, "w") as f:
            f.write("x")

        secret_path = "/var/lib/hsync-server/buckets/private/secret.txt"

        def boom(_path):
            raise OSError(f"Permission denied: '{secret_path}'")

        monkeypatch.setattr("os.remove", boom)

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/deletebucket3/files",
            data={"cid": "TestClientX1234567890", "filename": "x.txt"},
        )

        assert response.status_code == 500
        assert secret_path not in response.text
        details = response.json()["details"]
        assert "error" not in details
        assert details["reason"] == "OSError"
