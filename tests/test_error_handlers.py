"""
Tests for hsync_server.error_handlers module.
"""

import json

import pytest
from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.testclient import TestClient

from hsync_server.error_handlers import (
    EXCEPTION_STATUS_CODES,
    get_status_code_for_exception,
    global_exception_handler,
    hsync_exception_handler,
    http_exception_handler,
    register_exception_handlers,
    validation_exception_handler,
)
from hsync_server.exceptions import (
    AuthenticationError,
    FileOperationError,
    HSyncError,
    InvalidApiKeyError,
    MissingApiKeyError,
    ResourceNotFoundError,
    ValidationError,
)


class TestExceptionStatusCodes:
    """Tests for EXCEPTION_STATUS_CODES mapping."""

    def test_validation_error_status(self):
        """Test ValidationError maps to 400."""
        assert EXCEPTION_STATUS_CODES[ValidationError] == status.HTTP_400_BAD_REQUEST

    def test_resource_not_found_status(self):
        """Test ResourceNotFoundError maps to 404."""
        assert EXCEPTION_STATUS_CODES[ResourceNotFoundError] == status.HTTP_404_NOT_FOUND

    def test_invalid_api_key_status(self):
        """Test InvalidApiKeyError maps to 401."""
        assert EXCEPTION_STATUS_CODES[InvalidApiKeyError] == status.HTTP_401_UNAUTHORIZED

    def test_missing_api_key_status(self):
        """Test MissingApiKeyError maps to 403."""
        assert EXCEPTION_STATUS_CODES[MissingApiKeyError] == status.HTTP_403_FORBIDDEN

    def test_authentication_error_status(self):
        """Test AuthenticationError maps to 401."""
        assert EXCEPTION_STATUS_CODES[AuthenticationError] == status.HTTP_401_UNAUTHORIZED

    def test_file_operation_error_status(self):
        """Test FileOperationError maps to 500."""
        assert EXCEPTION_STATUS_CODES[FileOperationError] == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_base_hsync_error_status(self):
        """Test HSyncError maps to 500."""
        assert EXCEPTION_STATUS_CODES[HSyncError] == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestGetStatusCodeForException:
    """Tests for get_status_code_for_exception function."""

    def test_validation_error(self):
        """Test status code for ValidationError."""
        exc = ValidationError(message="Invalid data")
        assert get_status_code_for_exception(exc) == status.HTTP_400_BAD_REQUEST

    def test_resource_not_found_error(self):
        """Test status code for ResourceNotFoundError."""
        exc = ResourceNotFoundError(message="Not found")
        assert get_status_code_for_exception(exc) == status.HTTP_404_NOT_FOUND

    def test_authentication_error(self):
        """Test status code for AuthenticationError."""
        exc = AuthenticationError(message="Auth failed")
        assert get_status_code_for_exception(exc) == status.HTTP_401_UNAUTHORIZED

    def test_unknown_hsync_error(self):
        """Test status code for unknown HSyncError subclass."""
        exc = HSyncError(message="Unknown error")
        assert get_status_code_for_exception(exc) == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestHsyncExceptionHandler:
    """Tests for hsync_exception_handler."""

    @pytest.mark.asyncio
    async def test_handles_validation_error(self, mocker):
        """Test handling ValidationError returns 400."""
        request = mocker.MagicMock()
        exc = ValidationError(message="Invalid data")
        response = await hsync_exception_handler(request, exc)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        content = json.loads(response.body)
        assert content["error"] == "HSYNC-1000"

    @pytest.mark.asyncio
    async def test_handles_not_found_error(self, mocker):
        """Test handling ResourceNotFoundError returns 404."""
        request = mocker.MagicMock()
        exc = ResourceNotFoundError(message="Resource not found")
        response = await hsync_exception_handler(request, exc)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_handles_server_error(self, mocker):
        """Test handling FileOperationError returns 500."""
        request = mocker.MagicMock()
        exc = FileOperationError(message="File error")
        response = await hsync_exception_handler(request, exc)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestValidationExceptionHandler:
    """Tests for validation_exception_handler."""

    @pytest.mark.asyncio
    async def test_handles_validation_error(self, mocker):
        """Test handling Pydantic validation errors."""
        request = mocker.MagicMock()

        exc = mocker.MagicMock(spec=RequestValidationError)
        exc.errors.return_value = [
            {"loc": ["body", "field"], "msg": "field required", "type": "value_error.missing"}
        ]

        response = await validation_exception_handler(request, exc)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        content = json.loads(response.body)
        assert content["error"] == "HSYNC-1000"
        assert content["message"] == "Validation error"
        # The field-level detail stays in the log: echoing it back would spell
        # out the request schema to an unauthenticated caller.
        assert "details" not in content


class TestHttpExceptionHandler:
    """Framework-raised HTTP errors must not leak a framework-shaped body."""

    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/known")
        async def known():
            return {"ok": True}

        return app

    def test_unknown_path_is_a_generic_envelope(self):
        """A probe of an unmapped path gets no ``{"detail": ...}`` shape."""
        response = TestClient(self._app()).get("/does-not-exist")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json() == {"error": "Not Found"}

    def test_wrong_method_keeps_the_allow_header(self):
        """405 uses the generic envelope and keeps the HTTP ``Allow`` header."""
        response = TestClient(self._app()).post("/known")

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert response.json() == {"error": "Method Not Allowed"}
        assert "GET" in response.headers["Allow"]

    def test_deliberate_http_errors_keep_their_body(self):
        """401 with ``WWW-Authenticate`` is an API answer, not framework noise."""
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/private")
        async def private():
            raise StarletteHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )

        response = TestClient(app).get("/private")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json() == {"detail": "Not authenticated"}
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.asyncio
    async def test_handler_returns_the_given_headers(self, mocker):
        """The handler never drops headers the router attached."""
        request = mocker.MagicMock()
        exc = StarletteHTTPException(status_code=405, headers={"Allow": "GET, POST"})

        response = await http_exception_handler(request, exc)

        assert response.status_code == 405
        assert response.headers["allow"] == "GET, POST"


class TestGlobalExceptionHandler:
    """Tests for global_exception_handler."""

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self, mocker):
        """Test handling generic exceptions returns 500."""
        request = mocker.MagicMock()
        exc = Exception("Something went wrong")

        response = await global_exception_handler(request, exc)

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        content = json.loads(response.body)
        assert content["error"] == "HSYNC-5000"
        assert content["message"] == "An unexpected error occurred"

    @pytest.mark.asyncio
    async def test_handles_runtime_error(self, mocker):
        """Test handling RuntimeError."""
        request = mocker.MagicMock()
        exc = RuntimeError("Runtime error")

        response = await global_exception_handler(request, exc)

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestRegisterExceptionHandlers:
    """Tests for register_exception_handlers function."""

    def test_registers_all_handlers(self):
        """Test that the four core handlers are registered on a fresh app."""

        app = FastAPI()

        register_exception_handlers(app)

        # Four core handlers: StarletteHTTPException, HSyncError,
        # RequestValidationError, Exception.
        assert len(app.exception_handlers) >= 4
        handler_names = {h.__name__ for h in app.exception_handlers.values()}
        assert "http_exception_handler" in handler_names, (
            f"http_exception_handler not found in {handler_names}"
        )
        assert "hsync_exception_handler" in handler_names, (
            f"hsync_exception_handler not found in {handler_names}"
        )
        assert "validation_exception_handler" in handler_names, (
            f"validation_exception_handler not found in {handler_names}"
        )
        assert "global_exception_handler" in handler_names, (
            f"global_exception_handler not found in {handler_names}"
        )
