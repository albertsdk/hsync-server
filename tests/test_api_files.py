"""
Integration tests for file operations API.
"""

import os
import io

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
            data={"cid": "test-client-1", "path": "", "permissions": "-rw-r--r--"}
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
            data={"cid": "test-client-1", "path": "subdir/deep", "permissions": "-rw-r--r--"}
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
            data={"cid": "test-client-1", "path": "subdir", "permissions": "-rw-r--r--"}
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
            "/api/v1/buckets/downloadbucket/files/download",
            params={"filename": "download.txt"}
        )

        assert response.status_code == 200
        assert response.content == b"Download content"

    @pytest.mark.asyncio
    async def test_download_nonexistent_file(self, client: AsyncClient, temp_buckets_dir: str):
        """Download non-existent file returns 404."""
        bucket_path = os.path.join(temp_buckets_dir, "downloadbucket2")
        os.makedirs(bucket_path)

        response = await client.get(
            "/api/v1/buckets/downloadbucket2/files/download",
            params={"filename": "missing.txt"}
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
            data={"cid": "test-client-1", "filename": "todelete.txt"}
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
            data={"cid": "test-client-1", "filename": "missing.txt"}
        )

        assert response.status_code == 404

