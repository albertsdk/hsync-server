from .config import HSYNC_API_KEY
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from logging import getLogger
from typing import Annotated


LOG = getLogger("main")


def _handle_error(message: str, status_code: int):
    """Funzione helper per la gestione degli errori e il logging"""
    LOG.error(message)
    raise HTTPException(status_code=status_code, detail=message)


def mask_api_key(api_key: str) -> str:
    """Masks the API key to show only the first and last 5 characters."""
    return api_key[:3] + "*" * (len(api_key) - 10) + api_key[-3:]


def verify_api_key(
    token: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
):
    """Verifies the API key passed in the request header."""
    api_key = token.credentials
    if not api_key:
        return _handle_error("Undefined signature", status.HTTP_403_FORBIDDEN)

    if api_key != HSYNC_API_KEY:
        _handle_error(
            f"Invalid signature: {mask_api_key(api_key)}",
            status.HTTP_401_UNAUTHORIZED,
        )


# API Routers for authenticated and public routes
auth_router = APIRouter(
    tags=["Authenticated Router"],
    dependencies=[
        Depends(verify_api_key),
    ],
)
public_router = APIRouter(tags=["Public Router"])
