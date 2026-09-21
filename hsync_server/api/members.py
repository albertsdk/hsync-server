# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""
Membership and ownership API on the bucket path.

Endpoints (all require authentication):
- GET    /api/v1/buckets/{bucket}/members            — list members (owner/admin).
- POST   /api/v1/buckets/{bucket}/members            — grant member (owner/admin).
- DELETE /api/v1/buckets/{bucket}/members/{client_id} — revoke member (owner/admin).
- POST   /api/v1/buckets/{bucket}/owner               — transfer/reclaim (owner/admin).

Admin operates on any bucket; owner on their own; member gets 403; others 404.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, status
from fastapi.responses import JSONResponse
from hsync_common.protocol.rest import (
    REST_MEMBERS_CLIENT_PATH_TEMPLATE,
    REST_MEMBERS_SUFFIX,
    REST_OWNER_SUFFIX,
    rest_bucket_template,
)
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.api.responses import (
    account_not_found_response,
    conflict_response,
    not_found_response,
)
from hsync_server.api.utils import is_valid_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.auth_store import (
    ConflictError,
    ForbiddenError,
    TargetNotFoundError,
    get_auth_store,
)
from hsync_server.config import get_buckets_path
from hsync_server.exceptions import (
    BucketNotFoundError,
    InvalidBucketNameError,
    ValidationError,
)
from hsync_server.routers import auth_router


class GrantMemberRequest(BaseModel):
    client_id: str | None = Field(default=None, description="Target account id")
    name: str | None = Field(default=None, description="Target account name")


class TransferOwnerRequest(BaseModel):
    client_id: str | None = Field(default=None, description="Target account id")
    name: str | None = Field(default=None, description="Target account name")


def _check_owner_or_admin(bucket: str, ctx: AuthContext) -> None:
    """Raise 403 for members, 404 for non-members/nonexistent."""
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    bucket_dir = Path(get_buckets_path()) / bucket
    if not bucket_dir.exists():
        raise BucketNotFoundError(details={"bucket": bucket})
    if ctx.role == "admin":
        return
    store = get_auth_store()
    role = store.get_member_role(bucket, ctx.id)
    if role == "owner":
        return
    if role == "member":
        raise ForbiddenError()
    raise BucketNotFoundError(details={"bucket": bucket})


@auth_router.get(rest_bucket_template(REST_MEMBERS_SUFFIX))
async def list_members(
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """List members of a bucket (owner/admin only; member gets 403)."""
    _check_owner_or_admin(bucket, ctx)

    store = get_auth_store()
    members = store.get_members(bucket)
    result = []
    for m in members:
        acct = store.get_account(m.client_id)
        result.append(
            {
                "id": m.client_id,
                "name": acct.name if acct else "?",
                "role": m.role,
            }
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"bucket": bucket, "members": result}
    )


@auth_router.post(rest_bucket_template(REST_MEMBERS_SUFFIX))
async def grant_member(
    bucket: str,
    req: GrantMemberRequest,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Grant membership to an account (owner/admin only)."""
    _check_owner_or_admin(bucket, ctx)

    if (req.client_id is None) == (req.name is None):
        raise ValidationError("Provide exactly one of 'client_id' or 'name'.")

    store = get_auth_store()
    try:
        target = store.resolve_target(req.client_id, req.name)
    except TargetNotFoundError:
        return account_not_found_response()
    except ValueError as e:
        raise ValidationError(str(e)) from None

    try:
        store.grant_member(bucket, target, ctx.id, caller_is_admin=(ctx.role == "admin"))
    except ForbiddenError:
        raise
    except ConflictError as e:
        return conflict_response(str(e))

    log.info(f"Granted membership on '{bucket}' to {target.name} (by {ctx.name})")
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "bucket": bucket,
            "client_id": target.id,
            "name": target.name,
            "role": "member",
        },
    )


@auth_router.delete(REST_MEMBERS_CLIENT_PATH_TEMPLATE)
async def revoke_member(
    bucket: str,
    client_id: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Revoke membership (owner/admin only; last owner must transfer first)."""
    _check_owner_or_admin(bucket, ctx)

    store = get_auth_store()
    try:
        store.revoke_member(bucket, client_id, ctx.id, caller_is_admin=(ctx.role == "admin"))
    except TargetNotFoundError:
        return not_found_response("Account is not a member of this bucket.")
    except ConflictError as e:
        return conflict_response(str(e))

    # Close WS for that account on this bucket only.
    from hsync_server.api.connection_manager import bucket_manager

    bucket_manager.close_by_account_bucket(client_id, bucket)

    log.info(f"Revoked membership on '{bucket}' from {client_id} (by {ctx.name})")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"bucket": bucket, "client_id": client_id, "revoked": True},
    )


@auth_router.post(rest_bucket_template(REST_OWNER_SUFFIX))
async def transfer_or_reclaim_owner(
    bucket: str,
    req: TransferOwnerRequest,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Transfer ownership or reclaim an orphan bucket (owner/admin only).

    If the bucket has an owner, this transfers ownership (ex-owner stays
    member).  If the bucket has no owner (orphan), only admin can reclaim.
    """
    _check_owner_or_admin(bucket, ctx)

    if (req.client_id is None) == (req.name is None):
        raise ValidationError("Provide exactly one of 'client_id' or 'name'.")

    store = get_auth_store()
    try:
        target = store.resolve_target(req.client_id, req.name)
    except TargetNotFoundError:
        return account_not_found_response()
    except ValueError as e:
        raise ValidationError(str(e)) from None

    current_owner = store.get_owner(bucket)
    try:
        if current_owner is None:
            # Orphan reclaim — admin only (checked by _check_owner_or_admin
            # which allows admin; owner-of-nothing can't reach here).
            if ctx.role != "admin":
                raise ForbiddenError("Only admin can reclaim orphan buckets.")
            store.reclaim_orphan(bucket, target, caller_is_admin=True)
            action = "reclaimed"
        else:
            store.transfer_owner(bucket, target, ctx.id, caller_is_admin=(ctx.role == "admin"))
            action = "transferred"
    except ForbiddenError:
        raise
    except ConflictError as e:
        return conflict_response(str(e))

    log.info(f"Ownership of '{bucket}' {action} to {target.name} (by {ctx.name})")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "bucket": bucket,
            "owner": target.id,
            "name": target.name,
            "action": action,
        },
    )


log.info(f"Module {__name__} loaded")
