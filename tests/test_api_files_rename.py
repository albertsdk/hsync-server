# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Integration tests for the rename/move endpoint.

Covers ``PATCH …/files`` (``src``/``dst``): the primitive that lets a peer turn
a delete + re-upload into a zero-byte move after renaming a file while offline,
plus the causal bookkeeping (source tombstone, carried version) that keeps
offline peers from resurrecting the vacated path.
"""

import hashlib
import io
import os
from pathlib import Path

import pytest
from hsync_common.models.tombstone import tombstone_key
from httpx import AsyncClient


async def _upload(client: AsyncClient, bucket: str, name: str, content: bytes) -> None:
    """Upload one file through the public endpoint (records its version)."""
    directory, _, filename = name.rpartition("/")
    response = await client.post(
        f"/api/v1/buckets/{bucket}/files",
        files={"file": (filename, io.BytesIO(content), "text/plain")},
        data={
            "cid": "TestClientX1234567890",
            "path": directory,
            "permissions": "644",
            "sha256sum": hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 201, response.text


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class TestRenameFile:
    """PATCH /api/v1/buckets/{bucket}/files for a single file."""

    @pytest.mark.asyncio
    async def test_moves_the_file_and_creates_missing_parents(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renamebucket"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        content = b"payload"
        await _upload(client, bucket, "old.bin", content)

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={
                "src": "old.bin",
                "dst": "nested/new.bin",
                "cid": "TestClientX1234567890",
                "expected_sha256": _sha(content),
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["info"] == "Renamed successfully"
        assert body["file"]["filepath"] == "nested/new.bin"
        assert body["file"]["sha256sum"] == _sha(content)
        assert not Path(temp_buckets_dir, bucket, "old.bin").exists()
        assert Path(temp_buckets_dir, bucket, "nested", "new.bin").read_bytes() == content

    @pytest.mark.asyncio
    async def test_carries_the_version_to_the_new_path(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renameversion"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"versioned")

        before = (await client.get(f"/api/v1/buckets/{bucket}/files")).json()
        source_version = next(
            entry["version"] for entry in before if entry["filepath"] == "old.bin"
        )

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "old.bin", "dst": "new.bin"},
        )
        assert response.status_code == 200, response.text

        # A rename is not an edit: the stored clock travels unchanged.
        assert response.json()["version"]["vclock"] == source_version["vclock"]
        listing = (await client.get(f"/api/v1/buckets/{bucket}/files")).json()
        assert {entry["filepath"] for entry in listing} == {"new.bin"}
        assert listing[0]["version"]["vclock"] == source_version["vclock"]

    @pytest.mark.asyncio
    async def test_records_a_tombstone_for_the_vacated_path(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renametombstone"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"tombstoned")

        assert (
            await client.patch(
                f"/api/v1/buckets/{bucket}/files",
                params={"src": "old.bin", "dst": "new.bin"},
            )
        ).status_code == 200

        markers = (await client.get(f"/api/v1/buckets/{bucket}/tombstones")).json()
        assert tombstone_key("old.bin") in markers
        # The destination is populated, so no marker may linger there.
        assert tombstone_key("new.bin") not in markers

    @pytest.mark.asyncio
    async def test_applies_the_advertised_metadata(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renamemeta"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"meta")
        target_mtime = 1_700_000_000

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={
                "src": "old.bin",
                "dst": "new.bin",
                "mtime": target_mtime,
                "permissions": "600",
            },
        )

        assert response.status_code == 200, response.text
        moved = Path(temp_buckets_dir, bucket, "new.bin")
        assert int(moved.stat().st_mtime) == target_mtime
        assert oct(moved.stat().st_mode & 0o777) == "0o600"

    @pytest.mark.asyncio
    async def test_existing_destination_is_a_conflict(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renameconflict"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"old content")
        await _upload(client, bucket, "taken.bin", b"existing content")

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "old.bin", "dst": "taken.bin"},
        )

        assert response.status_code == 409
        # Nothing was overwritten: both paths still hold their own bytes.
        assert Path(temp_buckets_dir, bucket, "old.bin").read_bytes() == b"old content"
        assert Path(temp_buckets_dir, bucket, "taken.bin").read_bytes() == b"existing content"

    @pytest.mark.asyncio
    async def test_stale_checksum_is_a_conflict(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = "renamestale"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"current content")

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={
                "src": "old.bin",
                "dst": "new.bin",
                "expected_sha256": _sha(b"what the listing claimed"),
            },
        )

        assert response.status_code == 409
        assert Path(temp_buckets_dir, bucket, "old.bin").exists()
        assert not Path(temp_buckets_dir, bucket, "new.bin").exists()

    @pytest.mark.asyncio
    async def test_missing_source_is_not_found(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = "renamemissing"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "absent.bin", "dst": "new.bin"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_vacated_directory_keeps_its_sentinel(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        """An emptied source directory survives the next reconcile."""
        bucket = "renamesentinel"
        bucket_path = Path(temp_buckets_dir, bucket)
        (bucket_path / "sub").mkdir(parents=True)
        await _upload(client, bucket, "sub/old.bin", b"x")

        assert (
            await client.patch(
                f"/api/v1/buckets/{bucket}/files",
                params={"src": "sub/old.bin", "dst": "new.bin"},
            )
        ).status_code == 200

        assert (bucket_path / "sub" / ".hsynckeep").exists()

    @pytest.mark.asyncio
    async def test_path_traversal_is_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        bucket = "renametraversal"
        os.makedirs(os.path.join(temp_buckets_dir, bucket))
        await _upload(client, bucket, "old.bin", b"contained")

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "old.bin", "dst": "../escape.bin"},
        )

        assert response.status_code == 400
        assert not Path(temp_buckets_dir, "escape.bin").exists()

    @pytest.mark.asyncio
    async def test_symlink_rename_is_rejected(self, client: AsyncClient, temp_buckets_dir: str):
        """Links replicate structurally: a rename would have to be a link pair."""
        bucket = "renamelink"
        bucket_path = Path(temp_buckets_dir, bucket)
        bucket_path.mkdir()
        (bucket_path / "target.bin").write_bytes(b"target")
        os.symlink("target.bin", bucket_path / "link.bin")

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "link.bin", "dst": "moved.bin"},
        )

        assert response.status_code == 400
        assert (bucket_path / "link.bin").is_symlink()


class TestRenameDirectory:
    """PATCH …/files for a directory moves the whole tree without bytes."""

    @pytest.mark.asyncio
    async def test_moves_the_tree_and_carries_versions(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket = "renamedir"
        bucket_path = Path(temp_buckets_dir, bucket)
        (bucket_path / "tree").mkdir(parents=True)
        await _upload(client, bucket, "tree/one.bin", b"one")
        await _upload(client, bucket, "tree/two.bin", b"two")

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"src": "tree", "dst": "moved"},
        )

        assert response.status_code == 200, response.text
        assert not (bucket_path / "tree").exists()
        assert (bucket_path / "moved" / "one.bin").read_bytes() == b"one"
        assert (bucket_path / "moved" / "two.bin").read_bytes() == b"two"

        listing = (await client.get(f"/api/v1/buckets/{bucket}/files")).json()
        assert {entry["filepath"] for entry in listing} == {"moved/one.bin", "moved/two.bin"}
        assert all("version" in entry for entry in listing)

        markers = (await client.get(f"/api/v1/buckets/{bucket}/tombstones")).json()
        assert tombstone_key("tree/one.bin") in markers
        assert tombstone_key("tree/two.bin") in markers
