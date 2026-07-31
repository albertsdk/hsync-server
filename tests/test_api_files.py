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
        # Create bucket
        bucket_path = os.path.join(temp_buckets_dir, "uploadbucket")
        os.makedirs(bucket_path)

        # Upload file - cid is required
        file_content = b"Hello, World!"
        response = await client.post(
            "/api/v1/buckets/uploadbucket/files",
            files={"file": ("test.txt", io.BytesIO(file_content), "text/plain")},
            data={
                "cid": "test-client-1",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(file_content).hexdigest(),
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["info"] == "File uploaded successfully"

        # Verify file was created
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
                "cid": "test-client-1",
                "path": "subdir/deep",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"nested").hexdigest(),
            },
        )

        assert response.status_code == 201

        # Verify nested file was created
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
                "cid": "test-client-1",
                "path": "subdir",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"test").hexdigest(),
            },
        )

        # Upload succeeds and creates the directory structure
        assert response.status_code == 201

        # Verify file was created in the expected location
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
            data={"cid": "test-client-1", "filename": "todelete.txt"},
        )

        assert response.status_code == 200
        assert not os.path.exists(file_path)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete non-existent file returns 404."""
        bucket_path = os.path.join(temp_buckets_dir, "deletebucket2")
        os.makedirs(bucket_path)

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/deletebucket2/files",
            data={"cid": "test-client-1", "filename": "missing.txt"},
        )

        assert response.status_code == 404


class TestFileUploadValidation:
    """Tests for file upload validation."""

    @pytest.mark.asyncio
    async def test_upload_invalid_bucket_name(self, client: AsyncClient, temp_buckets_dir: str):
        """Upload to invalid bucket name returns 400."""
        response = await client.post(
            "/api/v1/buckets/invalid..bucket/files",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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
                "cid": "test-client-1",
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

        # Create content larger than limit
        large_content = b"x" * 1000

        with patch("hsync_server.api.files.upload.get_max_upload_size", return_value=100):
            response = await client.post(
                "/api/v1/buckets/sizelimitbucket/files",
                files={"file": ("large.txt", io.BytesIO(large_content), "text/plain")},
                data={
                    "cid": "test-client-1",
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
                    "cid": "test-client-1",
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
                    "cid": "test-client-1",
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
                "hsync_server.api.files.upload.get_settings",
                return_value=SimpleNamespace(min_free_disk_space=reserve),
            ),
            patch("shutil.disk_usage", return_value=fake_usage),
        ):
            response = await client.post(
                "/api/v1/buckets/diskbucket/files",
                files={"file": ("disk.txt", io.BytesIO(content), "text/plain")},
                data={
                    "cid": "test-client-1",
                    "path": "",
                    "permissions": "644",
                    "size": str(len(content)),
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
            )

        assert response.status_code != 201
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
                "hsync_server.api.files.upload.get_settings",
                return_value=SimpleNamespace(min_free_disk_space=reserve),
            ),
            patch("shutil.disk_usage", return_value=fake_usage),
        ):
            response = await client.post(
                "/api/v1/buckets/diskbucket2/files",
                files={"file": ("disk.txt", io.BytesIO(content), "text/plain")},
                data={
                    "cid": "test-client-1",
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

        # Create visible and OS cache files
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

        # Create visible and dotfiles
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


class TestFileDeleteValidation:
    """Tests for file delete validation."""

    @pytest.mark.asyncio
    async def test_delete_invalid_bucket_name(self, client: AsyncClient):
        """Delete from invalid bucket name returns 400."""
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/invalid..bucket/files",
            data={"cid": "test-client-1", "filename": "test.txt"},
        )
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_delete_bucket_not_found(self, client: AsyncClient):
        """Delete from non-existent bucket returns 404."""
        response = await client.request(
            "DELETE",
            "/api/v1/buckets/nonexistentbucket/files",
            data={"cid": "test-client-1", "filename": "test.txt"},
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
            data={"cid": "test-client-1", "filename": "../../../etc/passwd"},
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
                data={"cid": "test-client-1", "filename": "test.txt"},
            )

        assert response.status_code == 500
        assert "HSYNC-4003" in response.text


class TestTombstones:
    """Tests for the deletion-marker (tombstone) endpoint and lifecycle."""

    @pytest.mark.asyncio
    async def test_delete_records_tombstone(self, client: AsyncClient, temp_buckets_dir: str):
        """Deleting a file records a tombstone keyed by the hashed relative path."""
        import hashlib

        from hsync_common.models.tombstone import tombstone_key

        bucket_path = os.path.join(temp_buckets_dir, "tombbucket")
        os.makedirs(bucket_path)
        content = b"to be deleted"
        with open(os.path.join(bucket_path, "gone.txt"), "wb") as f:
            f.write(content)

        response = await client.request(
            "DELETE",
            "/api/v1/buckets/tombbucket/files",
            data={"cid": "test-client-1", "filename": "gone.txt"},
        )
        assert response.status_code == 200

        response = await client.get("/api/v1/buckets/tombbucket/tombstones")
        assert response.status_code == 200
        payload = response.json()

        key = tombstone_key("gone.txt")
        assert key in payload
        assert payload[key]["sha256"] == hashlib.sha256(content).hexdigest()
        assert payload[key]["ts"] > 0

    @pytest.mark.asyncio
    async def test_upload_clears_tombstone(self, client: AsyncClient, temp_buckets_dir: str):
        """Re-uploading a previously deleted path clears its tombstone."""
        from hsync_common.models.tombstone import tombstone_key

        bucket_path = os.path.join(temp_buckets_dir, "reusebucket")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "reuse.txt"), "wb") as f:
            f.write(b"original")

        await client.request(
            "DELETE",
            "/api/v1/buckets/reusebucket/files",
            data={"cid": "test-client-1", "filename": "reuse.txt"},
        )

        # Tombstone present after deletion
        payload = (await client.get("/api/v1/buckets/reusebucket/tombstones")).json()
        assert tombstone_key("reuse.txt") in payload

        # Re-create the same path via upload
        response = await client.post(
            "/api/v1/buckets/reusebucket/files",
            files={"file": ("reuse.txt", io.BytesIO(b"new content"), "text/plain")},
            data={
                "cid": "test-client-1",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(b"new content").hexdigest(),
            },
        )
        assert response.status_code == 201

        # Tombstone cleared
        payload = (await client.get("/api/v1/buckets/reusebucket/tombstones")).json()
        assert tombstone_key("reuse.txt") not in payload

    @pytest.mark.asyncio
    async def test_tombstones_empty_bucket(self, client: AsyncClient, temp_buckets_dir: str):
        """A bucket with no deletions returns an empty tombstone payload."""
        os.makedirs(os.path.join(temp_buckets_dir, "freshbucket"))
        response = await client.get("/api/v1/buckets/freshbucket/tombstones")
        assert response.status_code == 200
        assert response.json() == {}

    @pytest.mark.asyncio
    async def test_tombstones_invalid_bucket_name(self, client: AsyncClient):
        """Invalid bucket name returns 400."""
        response = await client.get("/api/v1/buckets/invalid..bucket/tombstones")
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_tombstones_bucket_not_found(self, client: AsyncClient):
        """Non-existent bucket returns 404."""
        response = await client.get("/api/v1/buckets/nosuchbucket/tombstones")
        assert response.status_code == 404
        assert "HSYNC-2001" in response.text


class TestUploadStatus:
    """Tests for the resumable upload-status endpoint."""

    @pytest.mark.asyncio
    async def test_status_new_when_nothing_uploaded(self, client: AsyncClient, temp_buckets_dir):
        os.makedirs(os.path.join(temp_buckets_dir, "statusbucket"))
        response = await client.get(
            "/api/v1/buckets/statusbucket/files/upload-status",
            params={"filename": "new.txt"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "new"
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_status_complete_when_checksum_matches(
        self, client: AsyncClient, temp_buckets_dir
    ):
        import hashlib

        from hsync_server.hashes import set_sha256_hash

        bucket_path = os.path.join(temp_buckets_dir, "statuscomplete")
        os.makedirs(bucket_path)
        content = b"already here"
        file_path = os.path.join(bucket_path, "done.txt")
        with open(file_path, "wb") as f:
            f.write(content)
        sha = hashlib.sha256(content).hexdigest()
        # The endpoint compares against the server's stored hash for this path.
        set_sha256_hash(file_path, sha)

        response = await client.get(
            "/api/v1/buckets/statuscomplete/files/upload-status",
            params={"filename": "done.txt", "sha256sum": sha},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "complete"

    @pytest.mark.asyncio
    async def test_status_resumable_when_partial_exists(
        self, client: AsyncClient, temp_buckets_dir
    ):
        bucket_path = os.path.join(temp_buckets_dir, "statusresume")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "big.txt.part"), "wb") as f:
            f.write(b"12345")

        response = await client.get(
            "/api/v1/buckets/statusresume/files/upload-status",
            params={"filename": "big.txt", "size": 100},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "resumable"
        assert data["offset"] == 5

    @pytest.mark.asyncio
    async def test_status_discards_oversized_partial(self, client: AsyncClient, temp_buckets_dir):
        bucket_path = os.path.join(temp_buckets_dir, "statusoversize")
        os.makedirs(bucket_path)
        partial = os.path.join(bucket_path, "big.txt.part")
        with open(partial, "wb") as f:
            f.write(b"toolong")

        response = await client.get(
            "/api/v1/buckets/statusoversize/files/upload-status",
            params={"filename": "big.txt", "size": 3},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "new"
        assert not os.path.exists(partial)

    @pytest.mark.asyncio
    async def test_status_invalid_bucket_name(self, client: AsyncClient):
        response = await client.get(
            "/api/v1/buckets/invalid..bucket/files/upload-status",
            params={"filename": "x.txt"},
        )
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_status_invalid_path(self, client: AsyncClient, temp_buckets_dir):
        os.makedirs(os.path.join(temp_buckets_dir, "statuspath"))
        response = await client.get(
            "/api/v1/buckets/statuspath/files/upload-status",
            params={"filename": "../../../etc/passwd"},
        )
        assert response.status_code == 400
        assert "HSYNC-1002" in response.text


class TestResumableUpload:
    """Tests for chunked/resumable upload finalization paths."""

    @pytest.mark.asyncio
    async def test_first_chunk_creates_partial(self, client: AsyncClient, temp_buckets_dir):
        """A first chunk smaller than the declared size is parked as a .part file."""
        bucket_path = os.path.join(temp_buckets_dir, "chunkbucket")
        os.makedirs(bucket_path)

        response = await client.post(
            "/api/v1/buckets/chunkbucket/files",
            files={"file": ("c.txt", io.BytesIO(b"12345"), "text/plain")},
            data={
                "cid": "c1",
                "path": "",
                "permissions": "644",
                "size": "100",
                "sha256sum": hashlib.sha256(b"12345").hexdigest(),
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["offset"] == 5
        assert body["remaining"] == 95
        assert os.path.exists(os.path.join(bucket_path, "c.txt.part"))

    @pytest.mark.asyncio
    async def test_resume_completes_upload(self, client: AsyncClient, temp_buckets_dir):
        """Appending the remaining bytes at the right offset finalizes the file."""
        bucket_path = os.path.join(temp_buckets_dir, "resumebucket")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "r.txt.part"), "wb") as f:
            f.write(b"AAAAA")

        response = await client.post(
            "/api/v1/buckets/resumebucket/files",
            files={"file": ("r.txt", io.BytesIO(b"BBBBB"), "text/plain")},
            data={
                "cid": "c1",
                "path": "",
                "permissions": "644",
                "offset": "5",
                "size": "10",
                "sha256sum": hashlib.sha256(b"AAAAABBBBB").hexdigest(),
            },
        )
        assert response.status_code == 201
        final = os.path.join(bucket_path, "r.txt")
        assert os.path.exists(final)
        with open(final, "rb") as f:
            assert f.read() == b"AAAAABBBBB"

    @pytest.mark.asyncio
    async def test_resume_offset_mismatch(self, client: AsyncClient, temp_buckets_dir):
        bucket_path = os.path.join(temp_buckets_dir, "mismatchbucket")
        os.makedirs(bucket_path)
        with open(os.path.join(bucket_path, "m.txt.part"), "wb") as f:
            f.write(b"AAAAA")  # 5 bytes on disk

        response = await client.post(
            "/api/v1/buckets/mismatchbucket/files",
            files={"file": ("m.txt", io.BytesIO(b"BB"), "text/plain")},
            data={
                "cid": "c1",
                "path": "",
                "offset": "10",
                "size": "20",
                "sha256sum": hashlib.sha256(b"BB").hexdigest(),
            },
        )
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_resume_without_partial_file(self, client: AsyncClient, temp_buckets_dir):
        os.makedirs(os.path.join(temp_buckets_dir, "nopartbucket"))
        response = await client.post(
            "/api/v1/buckets/nopartbucket/files",
            files={"file": ("n.txt", io.BytesIO(b"BB"), "text/plain")},
            data={
                "cid": "c1",
                "path": "",
                "offset": "5",
                "size": "20",
                "sha256sum": hashlib.sha256(b"BB").hexdigest(),
            },
        )
        assert response.status_code == 500
        # Error details must never embed the absolute partial-file path.
        body = response.json()
        body_str = str(body)
        assert temp_buckets_dir not in body_str
        assert "partial_file" not in body_str
