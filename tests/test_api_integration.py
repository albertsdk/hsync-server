"""
Integration tests for REST API endpoints.
"""

import os

import pytest
from httpx import AsyncClient


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self, client_no_auth: AsyncClient):
        """Health endpoint returns OK status with empty body."""
        response = await client_no_auth.get("/health")
        assert response.status_code == 200
        # Health endpoint returns an empty body (may be JSON-encoded as '""')
        assert response.content in (b"", b'""')

    @pytest.mark.asyncio
    async def test_health_check_no_auth_required(self, client_no_auth: AsyncClient):
        """Health endpoint does not require authentication (explicit test)."""
        response = await client_no_auth.get("/health")
        assert response.status_code == 200
        # Double-check: must not receive an auth challenge
        assert response.status_code not in (401, 403)

    @pytest.mark.asyncio
    async def test_health_check_content_type(self, client_no_auth: AsyncClient):
        """Health endpoint returns valid content type."""
        response = await client_no_auth.get("/health")
        content_type = response.headers.get("content-type", "")
        # Accept either text/plain or application/json
        assert "text/plain" in content_type or "application/json" in content_type


class TestBucketEndpoints:
    """Tests for bucket management endpoints."""

    @pytest.mark.asyncio
    async def test_create_bucket(self, client: AsyncClient, temp_buckets_dir: str):
        """Create a new bucket."""
        # bucket is a query parameter, not form data
        response = await client.post("/api/v1/buckets", params={"bucket": "testbucket"})
        assert response.status_code == 201
        data = response.json()
        assert data["info"] == "Bucket created successfully"

        # Verify bucket directory was created
        assert os.path.isdir(os.path.join(temp_buckets_dir, "testbucket"))

    @pytest.mark.asyncio
    async def test_create_bucket_already_exists(self, client: AsyncClient, temp_buckets_dir: str):
        """Creating existing bucket returns conflict."""
        # Create bucket directory first
        os.makedirs(os.path.join(temp_buckets_dir, "existing"))

        response = await client.post("/api/v1/buckets", params={"bucket": "existing"})
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_create_bucket_invalid_name(self, client: AsyncClient):
        """Invalid bucket names are rejected."""
        response = await client.post("/api/v1/buckets", params={"bucket": "../escape"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_get_bucket(self, client: AsyncClient, temp_buckets_dir: str):
        """Get bucket information."""
        # Create bucket
        bucket_path = os.path.join(temp_buckets_dir, "infobucket")
        os.makedirs(bucket_path)

        response = await client.get("/api/v1/buckets/infobucket")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "infobucket"
        assert "adate" in data
        assert "cdate" in data
        assert "mdate" in data

    @pytest.mark.asyncio
    async def test_get_bucket_not_found(self, client: AsyncClient):
        """Get non-existent bucket returns 404."""
        response = await client.get("/api/v1/buckets/nonexistent")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_bucket(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete a bucket."""
        # Create bucket
        bucket_path = os.path.join(temp_buckets_dir, "to-delete")
        os.makedirs(bucket_path)

        response = await client.delete("/api/v1/buckets/to-delete")
        assert response.status_code == 200
        data = response.json()
        assert data["info"] == "Bucket deleted successfully"
        assert data["bucket"] == "to-delete"

        # Verify bucket was deleted
        assert not os.path.exists(bucket_path)

    @pytest.mark.asyncio
    async def test_get_bucket_invalid_name(self, client: AsyncClient):
        """Get bucket with invalid name returns 400."""
        response = await client.get("/api/v1/buckets/invalid..bucket")
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_delete_bucket_invalid_name(self, client: AsyncClient):
        """Delete bucket with invalid name returns 400."""
        response = await client.delete("/api/v1/buckets/invalid..bucket")
        assert response.status_code == 400
        assert "HSYNC-1001" in response.text

    @pytest.mark.asyncio
    async def test_delete_bucket_not_found(self, client: AsyncClient):
        """Delete non-existent bucket returns 404."""
        response = await client.delete("/api/v1/buckets/nonexistent")
        assert response.status_code == 404
        assert "HSYNC-2001" in response.text

    @pytest.mark.asyncio
    async def test_delete_bucket_oserror(self, client: AsyncClient, temp_buckets_dir: str):
        """Delete bucket with OSError returns 500."""
        from unittest.mock import patch

        # Create bucket
        bucket_path = os.path.join(temp_buckets_dir, "oserror-bucket")
        os.makedirs(bucket_path)

        # Mock rmtree to raise OSError
        with patch("hsync_server.api.buckets.rmtree", side_effect=OSError("Permission denied")):
            response = await client.delete("/api/v1/buckets/oserror-bucket")

        assert response.status_code == 500
        assert "HSYNC-4003" in response.text


class TestAuthenticationRequired:
    """Tests for authentication requirement."""

    @pytest.mark.asyncio
    async def test_bucket_endpoints_require_auth(self, client_no_auth: AsyncClient):
        """Bucket endpoints require authentication."""
        # GET bucket
        response = await client_no_auth.get("/api/v1/buckets/test")
        assert response.status_code == 401

        # POST bucket
        response = await client_no_auth.post("/api/v1/buckets", data={"bucket": "test"})
        assert response.status_code == 401

        # DELETE bucket
        response = await client_no_auth.delete("/api/v1/buckets/test")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_file_endpoints_require_auth(self, client_no_auth: AsyncClient):
        """File endpoints require authentication."""
        # List files
        response = await client_no_auth.get("/api/v1/buckets/test/files")
        assert response.status_code == 401

        # Download file
        response = await client_no_auth.get(
            "/api/v1/buckets/test/files/download", params={"filename": "test.txt"}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_auth_token(self, test_app, temp_buckets_dir: str):
        """Invalid auth token is rejected."""
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer wrong-key"},
        ) as bad_client:
            response = await bad_client.get("/api/v1/buckets/test")
            assert response.status_code == 401
