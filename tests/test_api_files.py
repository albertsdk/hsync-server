"""
Integration tests for file operations API.
"""

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
            data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"},
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
            data={"cid": "test-client-1", "path": "subdir/deep", "permissions": "-rw-r--r--"},
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
            data={"cid": "test-client-1", "path": "subdir", "permissions": "-rw-r--r--"},
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
            data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"},
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
            data={"cid": "test-client-1", "path": "../../../tmp", "permissions": "-rw-r--r--"},
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
            data={"cid": "test-client-1", "path": "", "permissions": "invalid"},
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
                "permissions": "-rw-r--r--",
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
                "permissions": "-rw-r--r--",
                "sha256sum": sha256sum,
            },
        )
        assert response.status_code == 409
        assert "already present" in response.json()["info"]

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
                "permissions": "-rw-r--r--",
                "sha256sum": "wrongchecksum",
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
                data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"},
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
                data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"},
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
                data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"},
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
    async def test_list_files_skips_hidden(self, client: AsyncClient, temp_buckets_dir: str):
        """List files skips hidden files and directories."""
        bucket_path = os.path.join(temp_buckets_dir, "hiddenbucket")
        os.makedirs(bucket_path)

        # Create visible and hidden files
        with open(os.path.join(bucket_path, "visible.txt"), "w") as f:
            f.write("visible")
        with open(os.path.join(bucket_path, ".hidden"), "w") as f:
            f.write("hidden")

        response = await client.get("/api/v1/buckets/hiddenbucket/files")
        assert response.status_code == 200
        data = response.json()
        # Response is a list of file objects
        filenames = [f["filepath"] for f in data]
        assert any("visible.txt" in f for f in filenames)
        assert not any(".hidden" in f for f in filenames)


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
