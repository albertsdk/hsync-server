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


# ---------------------------------------------------------------------------
# Conflicted copy tests (ADR-0021 — Dropbox-style conflict resolution)
# ---------------------------------------------------------------------------


class TestConflictedCopy:
    """Tests for server-side conflicted copies on concurrent upload."""

    @pytest.mark.asyncio
    async def test_overwrite_different_content_creates_conflicted_copy(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Overwriting with different content preserves the old version."""
        import hashlib
        import io
        import json
        import os
        import re

        bucket_path = os.path.join(temp_buckets_dir, "conflictbucket")
        os.makedirs(bucket_path)

        original = b"version one -- original content"
        replacement = b"version two -- completely different"

        # Upload original with a vector clock from client A.
        r1 = await client.post(
            "/api/v1/buckets/conflictbucket/files",
            files={"file": ("doc.txt", io.BytesIO(original), "text/plain")},
            data={
                "cid": "TestClientA1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(original).hexdigest(),
                "vclock": json.dumps({"TestClientA1234567890": 1}),
                "version_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            },
        )
        assert r1.status_code == 201

        # Upload replacement from client B with a concurrent (non-dominating)
        # vector clock — a true divergence, so the tie-break decides and the
        # loser is preserved as a conflicted copy. The tie-break is by highest
        # SHA-256: the outcome (201 accepted_conflict_won vs 409 conflict_lost)
        # depends on which content hash wins.
        r2 = await client.post(
            "/api/v1/buckets/conflictbucket/files",
            files={"file": ("doc.txt", io.BytesIO(replacement), "text/plain")},
            data={
                "cid": "TestClientB1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(replacement).hexdigest(),
                "vclock": json.dumps({"TestClientB1234567890": 1}),
                "version_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
            },
        )
        assert r2.status_code in (201, 409), f"unexpected status {r2.status_code}: {r2.json()}"

        # Determine which content won the tie-break.
        orig_hash = hashlib.sha256(original).hexdigest()
        repl_hash = hashlib.sha256(replacement).hexdigest()
        # Original wins (kept at canonical path) iff its hash is higher;
        # the other content is preserved as the conflicted copy.
        winner_content = original if orig_hash > repl_hash else replacement

        # The original path has the winning content
        final_path = os.path.join(bucket_path, "doc.txt")
        assert os.path.exists(final_path)
        with open(final_path, "rb") as f:
            assert f.read() == winner_content

        # A conflicted copy exists with the losing content
        conflicted_files = [
            f for f in os.listdir(bucket_path) if f.startswith("doc") and "conflicted_copy" in f
        ]
        assert len(conflicted_files) == 1, f"expected 1 conflicted copy, found {conflicted_files}"
        # The cid in the name is the author of the losing content.
        loser_cid = "TestClientB1234567890" if orig_hash > repl_hash else "TestClientA1234567890"
        match = re.match(
            rf"doc_conflicted_copy_\d{{8}}T\d{{6}}Z_{loser_cid}\.txt",
            conflicted_files[0],
        )
        assert match is not None, (
            f"conflicted copy name does not match expected pattern: {conflicted_files[0]}"
        )

        loser_content = replacement if orig_hash > repl_hash else original
        conflicted_path = os.path.join(bucket_path, conflicted_files[0])
        with open(conflicted_path, "rb") as f:
            assert f.read() == loser_content

    @pytest.mark.asyncio
    async def test_overwrite_identical_content_no_conflicted_copy(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """Overwriting with identical SHA-256 returns 409, no copy created."""
        import hashlib
        import io
        import os

        bucket_path = os.path.join(temp_buckets_dir, "identicalbucket")
        os.makedirs(bucket_path)

        content = b"same content, same hash"

        # Upload first time
        r1 = await client.post(
            "/api/v1/buckets/identicalbucket/files",
            files={"file": ("note.txt", io.BytesIO(content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
        )
        assert r1.status_code == 201

        # Upload identical content again
        r2 = await client.post(
            "/api/v1/buckets/identicalbucket/files",
            files={"file": ("note.txt", io.BytesIO(content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
        )
        assert r2.status_code == 409

        # No conflicted copy should exist
        conflicted_files = [f for f in os.listdir(bucket_path) if "conflicted_copy" in f]
        assert len(conflicted_files) == 0

        # Original file intact
        final_path = os.path.join(bucket_path, "note.txt")
        with open(final_path, "rb") as f:
            assert f.read() == content

    @pytest.mark.asyncio
    async def test_first_upload_no_conflicted_copy(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """First upload to a non-existent path does not create a conflicted copy."""
        import hashlib
        import io
        import os

        bucket_path = os.path.join(temp_buckets_dir, "firstbucket")
        os.makedirs(bucket_path)

        content = b"brand new file"

        r = await client.post(
            "/api/v1/buckets/firstbucket/files",
            files={"file": ("new.txt", io.BytesIO(content), "text/plain")},
            data={
                "cid": "TestClientX1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
        )
        assert r.status_code == 201

        # No conflicted copy should exist (first upload, nothing to conflict with)
        conflicted_files = [f for f in os.listdir(bucket_path) if "conflicted_copy" in f]
        assert len(conflicted_files) == 0

    @pytest.mark.asyncio
    async def test_conflicted_copy_appears_in_file_listing(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """The conflicted copy is listed as a regular file for sync to all peers."""
        import hashlib
        import io
        import json
        import os

        bucket_path = os.path.join(temp_buckets_dir, "listingbucket")
        os.makedirs(bucket_path)

        original = b"first version"
        replacement = b"second version"

        # Upload v1 from client A with a vector clock.
        await client.post(
            "/api/v1/buckets/listingbucket/files",
            files={"file": ("data.csv", io.BytesIO(original), "text/csv")},
            data={
                "cid": "TestClientA1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(original).hexdigest(),
                "vclock": json.dumps({"TestClientA1234567890": 1}),
                "version_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            },
        )

        # Upload v2 from client B with a concurrent (non-dominating) clock.
        await client.post(
            "/api/v1/buckets/listingbucket/files",
            files={"file": ("data.csv", io.BytesIO(replacement), "text/csv")},
            data={
                "cid": "TestClientB1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(replacement).hexdigest(),
                "vclock": json.dumps({"TestClientB1234567890": 1}),
                "version_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
            },
        )

        # Both files appear in the listing
        listed = (await client.get("/api/v1/buckets/listingbucket/files")).json()
        names = [e["name"] for e in listed]
        assert "data.csv" in names
        assert any("conflicted_copy" in n for n in names), (
            f"conflicted copy not found in listing: {names}"
        )

    @pytest.mark.asyncio
    async def test_stale_upload_is_superseded_without_conflicted_copy(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """A causally dominated upload is rejected with 409 ``superseded`` and
        creates NO conflicted copy: the incoming content is an ancestor whose
        bytes are contained in the current version, so nothing is lost
        (ADR-0022 §4.6 / piano §4.1 "Stale puro")."""
        import hashlib
        import io
        import json
        import os

        bucket_path = os.path.join(temp_buckets_dir, "stalebucket")
        os.makedirs(bucket_path)

        current = b"current version"
        stale = b"stale ancestor version"

        # Upload the current version from client B (clock {B:2}).
        r1 = await client.post(
            "/api/v1/buckets/stalebucket/files",
            files={"file": ("doc.pdf", io.BytesIO(current), "application/pdf")},
            data={
                "cid": "TestClientB1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(current).hexdigest(),
                "vclock": json.dumps({"TestClientB1234567890": 2}),
                "version_id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
            },
        )
        assert r1.status_code == 201

        # Client A re-uploads an ancestor whose clock {B:1} is dominated by
        # the stored {B:2} (component-wise ≤ on every known key) — e.g. a
        # retry of an upload computed before B's second edit was learned.
        r2 = await client.post(
            "/api/v1/buckets/stalebucket/files",
            files={"file": ("doc.pdf", io.BytesIO(stale), "application/pdf")},
            data={
                "cid": "TestClientA1234567890",
                "path": "",
                "permissions": "644",
                "sha256sum": hashlib.sha256(stale).hexdigest(),
                "vclock": json.dumps({"TestClientB1234567890": 1}),
                "version_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            },
        )
        assert r2.status_code == 409
        body = r2.json()
        assert body.get("resolution") == "superseded"
        assert body.get("error") == "HSYNC-4005"

        # No conflicted copy is created: the stale bytes are discarded, the
        # current version stays untouched.
        final_path = os.path.join(bucket_path, "doc.pdf")
        with open(final_path, "rb") as f:
            assert f.read() == current
        conflicted_files = [f for f in os.listdir(bucket_path) if "conflicted_copy" in f]
        assert len(conflicted_files) == 0
