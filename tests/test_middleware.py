# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# Tests for middleware components.

import uuid

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient

from hsync_server.constants import (
    HEADER_CLIENT_NAME,
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    PROTOCOL_VERSION_REST,
)
from hsync_server.error_handlers import global_exception_handler
from hsync_server.logger import get_request_id
from hsync_server.middleware import (
    RequestIdMiddleware,
    TransferAwareGZipMiddleware,
    VersionMiddleware,
)


@pytest.fixture
def app_with_middleware():
    """Create a FastAPI app with RequestIdMiddleware."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/test")
    async def test_endpoint():
        # Return the current request ID from context
        return {"request_id": get_request_id()}

    return app


@pytest.fixture
def client(app_with_middleware):
    """Create a test client for the app."""
    return TestClient(app_with_middleware)


class TestRequestIdMiddleware:
    """Tests for RequestIdMiddleware."""

    def test_generates_request_id_when_not_provided(self, client):
        """Test that a request ID is generated when not in headers."""
        response = client.get("/test")

        assert response.status_code == 200
        assert "X-Request-ID" in response.headers
        # Verify it's a valid UUID
        request_id = response.headers["X-Request-ID"]
        uuid.UUID(request_id)  # Raises if invalid

    def test_uses_existing_request_id_from_header(self, client):
        """Test that existing X-Request-ID header is preserved."""
        custom_id = "custom-request-id-12345"
        response = client.get("/test", headers={"X-Request-ID": custom_id})

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == custom_id

    def test_request_id_available_in_context(self, client):
        """Test that request ID is available in context during request."""
        custom_id = "context-test-id"
        response = client.get("/test", headers={"X-Request-ID": custom_id})

        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == custom_id

    def test_request_id_cleared_after_request(self, client):
        """Test that request ID is cleared from context after request."""
        client.get("/test", headers={"X-Request-ID": "temp-id"})

        # After request completes, context should be cleared
        assert get_request_id() is None

    def test_multiple_requests_have_different_ids(self, client):
        """Test that each request gets a unique ID."""
        response1 = client.get("/test")
        response2 = client.get("/test")

        id1 = response1.headers["X-Request-ID"]
        id2 = response2.headers["X-Request-ID"]

        assert id1 != id2

    def test_request_id_format_is_uuid(self, client):
        """Test that generated request ID is a valid UUID."""
        response = client.get("/test")

        request_id = response.headers["X-Request-ID"]
        # This will raise ValueError if not a valid UUID
        parsed = uuid.UUID(request_id)
        assert str(parsed) == request_id


class TestRequestIdMiddlewareWithErrors:
    """Tests for RequestIdMiddleware error handling."""

    def test_request_id_cleared_on_exception(self):
        """Test that request ID is cleared even when exception occurs."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/error")
        async def error_endpoint():
            raise ValueError("Test error")

        client = TestClient(app, raise_server_exceptions=False)
        client.get("/error")

        # Context should still be cleared
        assert get_request_id() is None

    def test_response_has_request_id_on_error(self):
        """Test that response includes request ID even on 500 error."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        # Register the global exception handler so the X-Request-ID is
        # injected even when ServerErrorMiddleware bypasses the middleware.
        app.add_exception_handler(Exception, global_exception_handler)

        @app.get("/error")
        async def error_endpoint():
            raise ValueError("Test error")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/error", headers={"X-Request-ID": "error-test-id"})

        assert response.status_code == 500
        # The X-Request-ID must be present even on error responses
        assert "X-Request-ID" in response.headers
        assert response.headers["X-Request-ID"] == "error-test-id"


class TestRequestIdMiddlewareIntegration:
    """Integration tests for RequestIdMiddleware."""

    def test_works_with_multiple_endpoints(self):
        """Test middleware works across multiple endpoints."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/endpoint1")
        async def endpoint1():
            return {"id": get_request_id()}

        @app.get("/endpoint2")
        async def endpoint2():
            return {"id": get_request_id()}

        client = TestClient(app)

        resp1 = client.get("/endpoint1", headers={"X-Request-ID": "id1"})
        resp2 = client.get("/endpoint2", headers={"X-Request-ID": "id2"})

        assert resp1.json()["id"] == "id1"
        assert resp2.json()["id"] == "id2"


class TestVersionMiddleware:
    """Tests for VersionMiddleware."""

    def test_adds_version_headers(self):
        """X-Server-Version and X-Protocol-Version are added to responses."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test")

        assert response.status_code == 200
        assert response.headers[HEADER_SERVER_VERSION] == HSYNC_VERSION
        assert response.headers[HEADER_PROTOCOL_VERSION] == PROTOCOL_VERSION_REST

    def test_accepts_matching_protocol(self):
        """A request with the server's protocol version is accepted."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test", headers={HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST})

        assert response.status_code == 200
        assert response.headers[HEADER_PROTOCOL_VERSION] == PROTOCOL_VERSION_REST

    def test_rejects_mismatched_protocol(self):
        """A mismatched protocol version is hard-failed with 426."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={HEADER_PROTOCOL_VERSION: "api/999"})

        assert response.status_code == 426
        assert response.headers[HEADER_SERVER_VERSION] == HSYNC_VERSION
        assert response.headers[HEADER_PROTOCOL_VERSION] == PROTOCOL_VERSION_REST
        data = response.json()
        assert "error" in data
        assert data["client_protocol_version"] == "api/999"
        assert data["server_protocol_version"] == PROTOCOL_VERSION_REST

    def test_accepts_valid_client_name(self):
        """A request identifying a real product is accepted."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test", headers={HEADER_CLIENT_NAME: "hsync-client"})

        assert response.status_code == 200

    def test_rejects_malformed_client_name(self):
        """A client name that looks like a module file is rejected with 400."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test", headers={HEADER_CLIENT_NAME: "main_logic.py"})

        assert response.status_code == 400
        assert response.headers[HEADER_SERVER_VERSION] == HSYNC_VERSION
        assert response.headers[HEADER_PROTOCOL_VERSION] == PROTOCOL_VERSION_REST
        data = response.json()
        assert data["error"] == "Malformed client name"
        assert data["client_name"] == "main_logic.py"

    def test_rejects_ws_client_name_whitespace(self):
        """Whitespace or mixed-case product names are rejected."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        for bad_name in ("HSYNC Client", "hsync_client"):
            response = client.get("/test", headers={HEADER_CLIENT_NAME: bad_name})
            assert response.status_code == 400, bad_name

    def test_missing_client_name_accepted(self):
        """Requests without the client name (health checks) keep working."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test")

        assert response.status_code == 200

    def test_works_without_protocol_header(self):
        """Requests without the protocol header are accepted."""
        app = FastAPI()
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test")

        assert response.status_code == 200
        assert HEADER_PROTOCOL_VERSION in response.headers


class TestVersionMiddlewareIntegration:
    """Integration tests for VersionMiddleware with RequestIdMiddleware."""

    def test_both_middlewares_work_together(self):
        """Test that VersionMiddleware and RequestIdMiddleware work together."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"request_id": get_request_id()}

        client = TestClient(app)
        response = client.get(
            "/test",
            headers={
                HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST,
                "X-Request-ID": "test-request-id",
            },
        )

        assert response.status_code == 200
        assert "X-Request-ID" in response.headers
        assert response.headers["X-Request-ID"] == "test-request-id"
        assert HEADER_SERVER_VERSION in response.headers
        assert response.headers[HEADER_SERVER_VERSION] == HSYNC_VERSION

    def test_protocol_check_before_request_id(self):
        """A protocol mismatch returns 426 without setting a request ID."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(VersionMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"request_id": get_request_id()}

        # An incompatible protocol must 426 before any request handling.
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/test",
            headers={
                HEADER_PROTOCOL_VERSION: "api/999",
                "X-Request-ID": "should-not-appear",
            },
        )

        assert response.status_code == 426
        # Request ID should not be set since the protocol check failed early
        assert "X-Request-ID" not in response.headers


# =============================================================================
# TransferAwareGZipMiddleware
# =============================================================================

# Body served by the transfer routes in the tests below. Deliberately smaller
# than ``minimum_size``: Starlette's GZipMiddleware compresses the *first chunk*
# of a streamed response regardless of size, so a small transfer payload is the
# case that exposes the regression.
_TRANSFER_PAYLOAD = b"payload bytes that must arrive untouched\n"


def _gzip_app() -> FastAPI:
    """Build an app with the transfer-aware gzip middleware and its routes.

    Registers the two hsync endpoints that stream payload bytes (full-file
    download and delta literal download), a JSON listing above the gzip
    threshold, and a small JSON response, so one app exercises both sides of
    the boundary. The transfer routes stream their body, which is what the real
    endpoints do once an inner middleware has turned the ``FileResponse`` into a
    chunked stream — exactly the shape a plain ``GZipMiddleware`` compresses
    regardless of size.
    """

    async def _payload_stream():
        yield _TRANSFER_PAYLOAD

    app = FastAPI()
    app.add_middleware(TransferAwareGZipMiddleware, minimum_size=1000)

    @app.get("/api/v1/buckets/{bucket}/files/download")
    async def download_file():
        return StreamingResponse(_payload_stream())

    @app.get("/api/v1/buckets/{bucket}/files/delta/data/{token}")
    async def delta_data():
        return StreamingResponse(_payload_stream())

    @app.get("/api/v1/buckets/{bucket}/files")
    async def list_files():
        return JSONResponse({"entries": [f"entry-{i:04d}" for i in range(200)]})

    @app.get("/small")
    async def small():
        return JSONResponse({"ok": True})

    return app


class TestTransferAwareGZipMiddleware:
    """Gzip applies to API metadata, never to hsync transfer payloads."""

    def test_download_path_is_not_gzipped(self):
        """An advertised-gzip download still arrives as identity bytes."""
        client = TestClient(_gzip_app())

        response = client.get(
            "/api/v1/buckets/b/files/download",
            params={"filename": "payload.txt"},
            headers={"Accept-Encoding": "gzip"},
        )

        assert response.status_code == 200
        assert "Content-Encoding" not in response.headers
        assert response.content == _TRANSFER_PAYLOAD

    def test_delta_data_path_is_not_gzipped(self):
        """The delta literal payload is a transfer payload too."""
        client = TestClient(_gzip_app())

        response = client.get(
            "/api/v1/buckets/b/files/delta/data/tok-123",
            headers={"Accept-Encoding": "gzip"},
        )

        assert response.status_code == 200
        assert "Content-Encoding" not in response.headers
        assert response.content == _TRANSFER_PAYLOAD

    def test_api_listing_above_threshold_is_gzipped(self):
        """Metadata responses keep the size-based gzip behaviour."""
        client = TestClient(_gzip_app())

        response = client.get("/api/v1/buckets/b/files", headers={"Accept-Encoding": "gzip"})

        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "gzip"
        assert len(response.json()["entries"]) == 200

    def test_small_api_response_is_not_gzipped(self):
        """Responses below ``minimum_size`` stay uncompressed."""
        client = TestClient(_gzip_app())

        response = client.get("/small", headers={"Accept-Encoding": "gzip"})

        assert response.status_code == 200
        assert "Content-Encoding" not in response.headers
