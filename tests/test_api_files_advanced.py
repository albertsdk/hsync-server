"""
Integration tests for file operations API.
"""

import hashlib
import io
import os

import pytest
from httpx import AsyncClient


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
            data={"cid": "TestClientX1234567890", "filename": "gone.txt"},
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
            data={"cid": "TestClientX1234567890", "filename": "reuse.txt"},
        )

        # Tombstone present after deletion
        payload = (await client.get("/api/v1/buckets/reusebucket/tombstones")).json()
        assert tombstone_key("reuse.txt") in payload

        # Re-create the same path via upload
        response = await client.post(
            "/api/v1/buckets/reusebucket/files",
            files={"file": ("reuse.txt", io.BytesIO(b"new content"), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
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
                "cid": "TestClientXY1k4567890",
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
                "cid": "TestClientXY1k4567890",
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
                "cid": "TestClientXY1k4567890",
                "path": "",
                "offset": "10",
                "size": "20",
                "sha256sum": hashlib.sha256(b"BB").hexdigest(),
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_resume_without_partial_file(self, client: AsyncClient, temp_buckets_dir):
        os.makedirs(os.path.join(temp_buckets_dir, "nopartbucket"))
        response = await client.post(
            "/api/v1/buckets/nopartbucket/files",
            files={"file": ("n.txt", io.BytesIO(b"BB"), "text/plain")},
            data={
                "cid": "TestClientXY1k4567890",
                "path": "",
                "offset": "5",
                "size": "20",
                "sha256sum": hashlib.sha256(b"BB").hexdigest(),
            },
        )
        assert response.status_code == 400
        # Error details must never embed the absolute partial-file path.
        body = response.json()
        body_str = str(body)
        assert temp_buckets_dir not in body_str
        assert "partial_file" not in body_str
