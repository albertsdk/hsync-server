"""
Tests for hsync_server.error_handlers module.
"""

import json

import pytest
from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from hsync_server.error_handlers import (
    ErrorResponse,
    EXCEPTION_STATUS_CODES,
    get_status_code_for_exception,
    hsync_exception_handler,
    validation_exception_handler,
    global_exception_handler,
    register_exception_handlers,
)
from hsync_server.exceptions import (
    HSyncError,
    ValidationError,
    ResourceNotFoundError,
    AuthenticationError,
    FileOperationError,
    InvalidApiKeyError,
    MissingApiKeyError,
)


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_error_response_basic(self):
        """Test basic error response."""
        resp = ErrorResponse(error="HSYNC-1000", message="Test error")
        assert resp.error == "HSYNC-1000"
        assert resp.message == "Test error"
        assert resp.details is None

    def test_error_response_with_details(self):
        """Test error response with details."""
        details = {"field": "value", "count": 42}
        resp = ErrorResponse(error="HSYNC-1001", message="Test error", details=details)
        assert resp.details == details


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

        # Create a mock RequestValidationError
        exc = mocker.MagicMock(spec=RequestValidationError)
        exc.errors.return_value = [
            {"loc": ["body", "field"], "msg": "field required", "type": "value_error.missing"}
        ]

        response = await validation_exception_handler(request, exc)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        content = json.loads(response.body)
        assert content["error"] == "HSYNC-1000"
        assert content["message"] == "Validation error"


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
        """Test that all handlers are registered."""
        app = FastAPI()

        register_exception_handlers(app)

        # Check handlers are registered
        assert len(app.exception_handlers) >= 3
