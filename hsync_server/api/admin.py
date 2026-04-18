# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertobasso/hsync

"""
Admin REST API for HSYNC Server.

All endpoints require ``role=admin``.  Operations:
- Account CRUD (create, list, rotate-key, set-expiry, disable, enable, delete).
- Bucket inventory (FS ∪ membership) and create-on-behalf.

Consumers: ``hsync_server.main`` (router inclusion).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.auth import AuthContext, require_admin
from hsync_server.auth_store import (
    ConflictError,
    TargetNotFoundError,
    get_auth_store,
    parse_expiry,
    validate_name,
)
from hsync_server.exceptions import ValidationError
from hsync_server.rate_limiter import RATE_LIMIT_ADMIN, limiter

# Sub-router under /api/v1/admin — all endpoints inherit auth_router's
# verify_api_key dependency, plus require_admin.
admin_router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin"],
    dependencies=[Depends(require_admin)],
)


# =============================================================================
# Request models
# =============================================================================


class CreateClientRequest(BaseModel):
    name: str = Field(..., description="Account name (1-64 chars, alphanumeric with . _ -)")
    role: str = Field(default="client", description="Account role: 'admin' or 'client'")
    expires_at: str | None = Field(default=None, description="RFC3339 UTC expiry timestamp")
    valid_days: int | None = Field(default=None, ge=1, description="Key validity in days (≥1)")


class RotateKeyRequest(BaseModel):
    grace_seconds: int | None = Field(
        default=None, ge=1, description="Grace period for old key (≥1 second)"
    )
    expires_at: str | None = Field(default=None, description="RFC3339 UTC expiry for the new key")
    valid_days: int | None = Field(default=None, ge=1, description="Key validity in days (≥1)")


class SetExpiryRequest(BaseModel):
    expires_at: str | None = Field(default=None, description="RFC3339 UTC expiry timestamp")
    valid_days: int | None = Field(default=None, ge=1, description="Key validity in days (≥1)")
    none: bool = Field(default=False, description="Remove expiry")


class CreateBucketRequest(BaseModel):
    bucket: str = Field(..., description="Bucket name")
    owner: str = Field(..., description="Owner account id or name")


# =============================================================================
# Helpers
# =============================================================================


def _parse_expiry(expires_at: str | None, valid_days: int | None) -> int | None:
    """Convert expires_at (RFC3339) or valid_days to Unix UTC, or None."""
    try:
        return parse_expiry(expires_at, valid_days)
    except ValueError as e:
        raise ValidationError(str(e)) from None


def _account_to_dict(a) -> dict:
    """Serialize an AccountRow for API responses (never includes hash/secret)."""
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "status": a.status,
        "key_prefix": a.key_prefix,
        "key_generation": a.key_generation,
        "key_expires_at": a.key_expires_at,
        "created_at": a.created_at,
        "last_used_at": a.last_used_at,
    }


# =============================================================================
# Account endpoints
# =============================================================================


@admin_router.get("/clients")
@limiter.limit(RATE_LIMIT_ADMIN)
async def list_clients(request: Request) -> JSONResponse:
    """List all accounts (never includes hashes or secrets)."""
    store = get_auth_store()
    accounts = store.list_accounts()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"clients": [_account_to_dict(a) for a in accounts]},
    )


@admin_router.post("/clients", status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_ADMIN)
async def create_client(request: Request, req: CreateClientRequest) -> JSONResponse:
    """Create a new account. Returns the token once."""
    try:
        validate_name(req.name)
    except ValueError as e:
        raise ValidationError(str(e)) from None

    if req.role not in ("admin", "client"):
        raise ValidationError("Role must be 'admin' or 'client'.")

    expires_at = _parse_expiry(req.expires_at, req.valid_days)

    store = get_auth_store()
    try:
        account_id, token = store.create_account(req.name, req.role, expires_at)
    except ConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"error": "HSYNC-3004", "message": str(e)}
        )
    except ValueError as e:
        raise ValidationError(str(e)) from None

    log.info(f"Admin created account '{req.name}' (id={account_id}, role={req.role})")
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": account_id,
            "name": req.name,
            "role": req.role,
            "token": token,
            "info": "Save this token now: it cannot be recovered.",
        },
    )


@admin_router.post("/clients/{client_id}/rotate-key")
@limiter.limit(RATE_LIMIT_ADMIN)
async def rotate_key(request: Request, client_id: str, req: RotateKeyRequest) -> JSONResponse:
    """Rotate the account's key. Returns the new token once."""
    expires_at = _parse_expiry(req.expires_at, req.valid_days)

    store = get_auth_store()
    try:
        token = store.rotate_key(client_id, grace_seconds=req.grace_seconds, expires_at=expires_at)
    except TargetNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Account not found."},
        )

    log.info(f"Rotated key for account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "token": token,
            "info": "Save this token now: it cannot be recovered.",
        },
    )


@admin_router.patch("/clients/{client_id}/expiry")
@limiter.limit(RATE_LIMIT_ADMIN)
async def set_expiry(request: Request, client_id: str, req: SetExpiryRequest) -> JSONResponse:
    """Set, extend, or remove the key expiry."""
    if req.none:
        expires_at = None
    else:
        expires_at = _parse_expiry(req.expires_at, req.valid_days)
        if expires_at is None and not req.none:
            raise ValidationError("Provide 'expires_at', 'valid_days', or 'none'.")

    store = get_auth_store()
    try:
        store.set_expiry(client_id, expires_at)
    except TargetNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Account not found."},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": client_id, "key_expires_at": expires_at},
    )


@admin_router.post("/clients/{client_id}/disable")
@limiter.limit(RATE_LIMIT_ADMIN)
async def disable_client(
    request: Request,
    client_id: str,
    ctx: Annotated[AuthContext, Depends(require_admin)],
) -> JSONResponse:
    """Disable an account (blocks auth, closes WS). Self-disable → 409."""
    if client_id == ctx.id:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "HSYNC-3004", "message": "Cannot disable your own account."},
        )
    store = get_auth_store()
    try:
        store.disable(client_id)
    except TargetNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Account not found."},
        )
    except ConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "HSYNC-3004", "message": str(e)},
        )

    # Close WS connections for this account.
    _close_ws_by_account(client_id)

    log.info(f"Disabled account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"id": client_id, "status": "disabled"}
    )


@admin_router.post("/clients/{client_id}/enable")
@limiter.limit(RATE_LIMIT_ADMIN)
async def enable_client(request: Request, client_id: str) -> JSONResponse:
    """Enable a previously disabled account."""
    store = get_auth_store()
    try:
        store.enable(client_id)
    except TargetNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Account not found."},
        )

    log.info(f"Enabled account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"id": client_id, "status": "active"}
    )


@admin_router.delete("/clients/{client_id}")
@limiter.limit(RATE_LIMIT_ADMIN)
async def delete_client(
    request: Request,
    client_id: str,
    cascade: bool = False,
) -> JSONResponse:
    """Delete an account. Use ?cascade=true to delete owned buckets."""
    store = get_auth_store()
    try:
        store.delete_account(client_id, cascade=cascade)
    except TargetNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Account not found."},
        )
    except ConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "HSYNC-3004", "message": str(e)},
        )

    # Close WS connections for this account.
    _close_ws_by_account(client_id)

    log.info(f"Deleted account {client_id}")
    return JSONResponse(status_code=status.HTTP_200_OK, content={"id": client_id, "deleted": True})


# =============================================================================
# Bucket inventory
# =============================================================================


@admin_router.get("/buckets")
@limiter.limit(RATE_LIMIT_ADMIN)
async def list_all_buckets(request: Request) -> JSONResponse:
    """List all buckets (FS ∪ membership), including orphans and ghosts."""
    store = get_auth_store()
    buckets = store.list_all_buckets()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"buckets": buckets},
    )


@admin_router.post("/buckets", status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_ADMIN)
async def create_bucket_on_behalf(request: Request, req: CreateBucketRequest) -> JSONResponse:
    """Create a bucket on behalf of a specified owner (admin only)."""
    from pathlib import Path

    from hsync_server.api.utils import is_valid_bucket
    from hsync_server.config import get_buckets_path

    if not is_valid_bucket(req.bucket):
        from hsync_server.exceptions import InvalidBucketNameError

        raise InvalidBucketNameError(details={"bucket": req.bucket})

    store = get_auth_store()
    # Resolve owner by id or name.
    target = store.get_account_by_name(req.owner) or store.get_account(req.owner)
    if target is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "HSYNC-2000", "message": "Owner account not found."},
        )
    if target.status != "active":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "HSYNC-3004", "message": "Owner account is disabled."},
        )

    bucket_dir = Path(get_buckets_path()) / req.bucket
    try:
        bucket_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "HSYNC-3004", "message": "Bucket already exists."},
        )

    try:
        store.set_owner_on_create(req.bucket, target.id)
    except Exception:
        log.error(f"Failed to record ownership for bucket '{req.bucket}'", exc_info=True)

    log.info(f"Admin created bucket '{req.bucket}' on behalf of {target.name}")
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "bucket": req.bucket,
            "owner": target.id,
            "info": "Bucket created successfully.",
        },
    )


# =============================================================================
# WS close helper
# =============================================================================


def _close_ws_by_account(account_id: str) -> None:
    """Close all WS connections for an account (best-effort, local worker)."""
    from hsync_server.api.connection_manager import bucket_manager
    from hsync_server.auth import _cache_evict_account

    _cache_evict_account(account_id)
    bucket_manager.close_by_account(account_id)


log.info(f"Module {__name__} loaded")
