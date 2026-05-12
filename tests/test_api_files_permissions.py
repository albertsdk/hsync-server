# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Integration tests for the metadata-only permission endpoints.

Covers the dedicated ``PATCH …/files/permissions`` endpoint (chmod without
any file payload) and the ``GET …/dirs`` directory-metadata listing used by
the download-only directory-permission converge.
"""

import hashlib
import io
import os
from pathlib import Path

import pytest
from httpx import AsyncClient


async def _upload(client: AsyncClient, bucket: str, name: str, content: bytes) -> None:
    """Upload one file through the public endpoint."""
    response = await client.post(
        f"/api/v1/buckets/{bucket}/files",
        files={"file": (name, io.BytesIO(content), "text/plain")},
        data={
            "cid": "TestClientX1234567890",
            "permissions": "644",
            "sha256sum": hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 201, response.text


class TestPatchFilePermissions:
    """PATCH /api/v1/buckets/{bucket}/files/permissions behaviour."""

    @pytest.mark.asyncio
    async def test_patch_updates_permissions_without_payload(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        # Given an uploaded file
        bucket_path = os.path.join(temp_buckets_dir, "permbucket")
        os.makedirs(bucket_path)
        await _upload(client, "permbucket", "perm.bin", b"same bytes")

        # When the permissions are patched through the metadata-only endpoint
        response = await client.patch(
            "/api/v1/buckets/permbucket/files/permissions",
            params={
                "filename": "perm.bin",
                "permissions": "600",
                "cid": "TestClientX1234567890",
            },
        )

        # Then the endpoint reports success and applies the mode on disk
        assert response.status_code == 200, response.text
        assert response.json()["info"] == "Permissions updated"
        mode = oct(Path(bucket_path, "perm.bin").stat().st_mode & 0o777)
        assert mode == "0o600"

    @pytest.mark.asyncio
    async def test_patch_same_permissions_is_idempotent(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        bucket_path = os.path.join(temp_buckets_dir, "permbucket2")
        os.makedirs(bucket_path)
        await _upload(client, "permbucket2", "idem.bin", b"idem")

        response = await client.patch(
            "/api/v1/buckets/permbucket2/files/permissions",
            params={"filename": "idem.bin", "permissions": "644"},
        )

        assert response.status_code == 200
        assert response.json()["info"] == "Permissions already current"

    @pytest.mark.asyncio
    async def test_patch_strips_special_bits(self, client: AsyncClient, temp_buckets_dir: str):
        bucket_path = os.path.join(temp_buckets_dir, "permbucket3")
        os.makedirs(bucket_path)
        await _upload(client, "permbucket3", "setuid.bin", b"x")

        response = await client.patch(
            "/api/v1/buckets/permbucket3/files/permissions",
            params={"filename": "setuid.bin", "permissions": "4755"},
        )

        assert response.status_code == 200
        # setuid must never be applied on behalf of a remote peer
        mode = Path(bucket_path, "setuid.bin").stat().st_mode & 0o7777
        assert mode == 0o755

    @pytest.mark.asyncio
    async def test_patch_missing_file_returns_404(self, client: AsyncClient, temp_buckets_dir: str):
        os.makedirs(os.path.join(temp_buckets_dir, "permbucket4"))

        response = await client.patch(
            "/api/v1/buckets/permbucket4/files/permissions",
            params={"filename": "ghost.bin", "permissions": "600"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_invalid_permission_format_returns_400(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        os.makedirs(os.path.join(temp_buckets_dir, "permbucket5"))

        response = await client.patch(
            "/api/v1/buckets/permbucket5/files/permissions",
            params={"filename": "x.bin", "permissions": "888"},
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_traversal_path_is_rejected(
        self, client: AsyncClient, temp_buckets_dir: str
    ):
        os.makedirs(os.path.join(temp_buckets_dir, "permbucket6"))

        response = await client.patch(
            "/api/v1/buckets/permbucket6/files/permissions",
            params={"filename": "../escape.bin", "permissions": "600"},
        )

        assert response.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_patch_nested_path(self, client: AsyncClient, temp_buckets_dir: str):
        bucket_path = os.path.join(temp_buckets_dir, "permbucket7")
        os.makedirs(bucket_path)
        await _upload(client, "permbucket7", "nested.bin", b"nested")
        os.makedirs(os.path.join(bucket_path, "a", "b"), exist_ok=True)
        nested = os.path.join(bucket_path, "a", "b", "nested.bin")
        os.replace(os.path.join(bucket_path, "nested.bin"), nested)

        response = await client.patch(
            "/api/v1/buckets/permbucket7/files/permissions",
            params={"filename": "a/b/nested.bin", "permissions": "640"},
        )

        assert response.status_code == 200
        assert oct(Path(nested).stat().st_mode & 0o777) == "0o640"


class TestListDirs:
    """GET /api/v1/buckets/{bucket}/dirs behaviour."""

    @pytest.mark.asyncio
    async def test_lists_directory_permissions(self, client: AsyncClient, temp_buckets_dir: str):
        bucket_path = os.path.join(temp_buckets_dir, "dirbucket")
        sub = os.path.join(bucket_path, "sub", "deep")
        os.makedirs(sub)
        os.chmod(sub, 0o700)

        response = await client.get("/api/v1/buckets/dirbucket/dirs")

        assert response.status_code == 200
        entries = {e["path"]: e["permissions"] for e in response.json()}
        assert "sub/deep" in entries
        assert entries["sub/deep"] == "700"

    @pytest.mark.asyncio
    async def test_missing_bucket_returns_404(self, client: AsyncClient):
        response = await client.get("/api/v1/buckets/ghostbucket/dirs")
        assert response.status_code == 404
