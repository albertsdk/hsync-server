# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

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
from hsync_common.protocol.rest import (
    REST_ADMIN_BUCKETS_PATH,
    REST_ADMIN_CLIENT_DISABLE_PATH,
    REST_ADMIN_CLIENT_ENABLE_PATH,
    REST_ADMIN_CLIENT_EXPIRY_PATH,
    REST_ADMIN_CLIENT_PATH,
    REST_ADMIN_CLIENT_ROTATE_KEY_PATH,
    REST_ADMIN_CLIENTS_PATH,
)
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.api.responses import (
    account_not_found_response,
    conflict_response,
    not_found_response,
)
from hsync_server.api.utils import is_valid_bucket
from hsync_server.auth import AuthContext, require_admin
from hsync_server.auth_store import (
    AuthStore,
    ConflictError,
    TargetNotFoundError,
    get_auth_store,
    parse_expiry,
    validate_name,
)
from hsync_server.bucket_provisioning import create_bucket
from hsync_server.exceptions import InvalidBucketNameError, ValidationError
from hsync_server.rate_limiter import RATE_LIMIT_ADMIN, limiter

# Admin sub-router — all endpoints inherit auth_router's verify_api_key
# dependency, plus require_admin. Paths come from the shared REST SSOT.
admin_router = APIRouter(
    tags=["Admin"],
    dependencies=[Depends(require_admin)],
)

# Injected per request: the AuthStore singleton (reset/rebind-aware). One
# dependency instead of a ``store = get_auth_store()`` line in every handler.
StoreDep = Annotated[AuthStore, Depends(get_auth_store)]


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


@admin_router.get(REST_ADMIN_CLIENTS_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def list_clients(request: Request, store: StoreDep) -> JSONResponse:
    """List all accounts (never includes hashes or secrets)."""
    accounts = store.list_accounts()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"clients": [_account_to_dict(a) for a in accounts]},
    )


@admin_router.post(REST_ADMIN_CLIENTS_PATH, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_ADMIN)
async def create_client(
    request: Request, req: CreateClientRequest, store: StoreDep
) -> JSONResponse:
    """Create a new account. Returns the token once."""
    try:
        validate_name(req.name)
    except ValueError as e:
        raise ValidationError(str(e)) from None

    if req.role not in ("admin", "client"):
        raise ValidationError("Role must be 'admin' or 'client'.")

    expires_at = _parse_expiry(req.expires_at, req.valid_days)

    try:
        account_id, token = store.create_account(req.name, req.role, expires_at)
    except ConflictError as e:
        return conflict_response(str(e))
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


@admin_router.post(REST_ADMIN_CLIENT_ROTATE_KEY_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def rotate_key(
    request: Request, client_id: str, req: RotateKeyRequest, store: StoreDep
) -> JSONResponse:
    """Rotate the account's key. Returns the new token once."""
    expires_at = _parse_expiry(req.expires_at, req.valid_days)

    try:
        token = store.rotate_key(client_id, grace_seconds=req.grace_seconds, expires_at=expires_at)
    except TargetNotFoundError:
        return account_not_found_response()

    log.info(f"Rotated key for account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "token": token,
            "info": "Save this token now: it cannot be recovered.",
        },
    )


@admin_router.patch(REST_ADMIN_CLIENT_EXPIRY_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def set_expiry(
    request: Request, client_id: str, req: SetExpiryRequest, store: StoreDep
) -> JSONResponse:
    """Set, extend, or remove the key expiry."""
    if req.none:
        expires_at = None
    else:
        expires_at = _parse_expiry(req.expires_at, req.valid_days)
        if expires_at is None:
            raise ValidationError("Provide 'expires_at', 'valid_days', or 'none'.")

    try:
        store.set_expiry(client_id, expires_at)
    except TargetNotFoundError:
        return account_not_found_response()

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"id": client_id, "key_expires_at": expires_at},
    )


@admin_router.post(REST_ADMIN_CLIENT_DISABLE_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def disable_client(
    request: Request,
    client_id: str,
    ctx: Annotated[AuthContext, Depends(require_admin)],
    store: StoreDep,
) -> JSONResponse:
    """Disable an account (blocks auth, closes WS). Self-disable → 409."""
    if client_id == ctx.id:
        return conflict_response("Cannot disable your own account.")
    try:
        store.disable(client_id)
    except TargetNotFoundError:
        return account_not_found_response()
    except ConflictError as e:
        return conflict_response(str(e))

    _close_ws_by_account(client_id)

    log.info(f"Disabled account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"id": client_id, "status": "disabled"}
    )


@admin_router.post(REST_ADMIN_CLIENT_ENABLE_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def enable_client(request: Request, client_id: str, store: StoreDep) -> JSONResponse:
    """Enable a previously disabled account."""
    try:
        store.enable(client_id)
    except TargetNotFoundError:
        return account_not_found_response()

    log.info(f"Enabled account {client_id}")
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"id": client_id, "status": "active"}
    )


@admin_router.delete(REST_ADMIN_CLIENT_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def delete_client(
    request: Request,
    client_id: str,
    store: StoreDep,
    cascade: bool = False,
) -> JSONResponse:
    """Delete an account. Use ?cascade=true to delete owned buckets."""
    try:
        store.delete_account(client_id, cascade=cascade)
    except TargetNotFoundError:
        return account_not_found_response()
    except ConflictError as e:
        return conflict_response(str(e))

    _close_ws_by_account(client_id)

    log.info(f"Deleted account {client_id}")
    return JSONResponse(status_code=status.HTTP_200_OK, content={"id": client_id, "deleted": True})


# =============================================================================
# Bucket inventory
# =============================================================================


@admin_router.get(REST_ADMIN_BUCKETS_PATH)
@limiter.limit(RATE_LIMIT_ADMIN)
async def list_all_buckets(request: Request, store: StoreDep) -> JSONResponse:
    """List all buckets (FS ∪ membership), including orphans and ghosts."""
    buckets = store.list_all_buckets()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"buckets": buckets},
    )


@admin_router.post(REST_ADMIN_BUCKETS_PATH, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_ADMIN)
async def create_bucket_on_behalf(
    request: Request, req: CreateBucketRequest, store: StoreDep
) -> JSONResponse:
    """Create a bucket on behalf of a specified owner (admin only)."""
    if not is_valid_bucket(req.bucket):
        raise InvalidBucketNameError(details={"bucket": req.bucket})

    # Resolve owner by id or name.
    target = store.resolve_account(req.owner)
    if target is None:
        return not_found_response("Owner account not found.")
    if target.status != "active":
        return conflict_response("Owner account is disabled.")

    try:
        create_bucket(req.bucket, owner_account_id=target.id, owner_name=target.name)
    except ConflictError:
        return conflict_response("Bucket already exists.")

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
