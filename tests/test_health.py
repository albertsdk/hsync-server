"""
Tests for hsync_server.api.health module.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from hsync_server.routers import public_router
from hsync_server.api import health  # noqa: F401 - imports endpoint


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    test_app = FastAPI()
    test_app.include_router(public_router)
    return test_app


@pytest.fixture
async def client(app):
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestHealthEndpoint:
    """Tests for the health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_returns_200(self, client):
        """Test that health endpoint returns 200 OK."""
        response = await client.get("/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_check_returns_empty_body(self, client):
        """Test that health endpoint returns empty response body."""
        response = await client.get("/health")
        assert response.text == ""

    @pytest.mark.asyncio
    async def test_health_check_content_type(self, client):
        """Test that health endpoint returns plain text content type."""
        response = await client.get("/health")
        assert "text/plain" in response.headers.get("content-type", "")

