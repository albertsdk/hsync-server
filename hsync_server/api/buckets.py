# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertobasso/hsync

"""
Bucket operations API for HSYNC Server.

REST endpoints for bucket management:
- GET  /api/v1/buckets/{bucket}   — info (owner or member or admin).
- POST /api/v1/buckets             — create (creator becomes owner).
- DELETE /api/v1/buckets/{bucket}  — delete (admin only; cleans DB+FS).
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, status
from fastapi.responses import JSONResponse
from hsync_common.time_utils import to_iso
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.auth import AuthContext, require_admin, verify_api_key
from hsync_server.bucket_provisioning import create_bucket as _create_bucket
from hsync_server.bucket_provisioning import delete_bucket as _delete_bucket
from hsync_server.config import get_buckets_path
from hsync_server.exceptions import ConflictError
from hsync_server.routers import auth_router


def _bucket_path(bucket: str) -> str:
    """Return the filesystem path for *bucket* inside the configured storage root."""
    return f"{get_buckets_path()}/{bucket}"


@auth_router.get(
    "/api/v1/buckets/{bucket}",
    name="Get bucket information",
    summary="Get bucket information",
    description="Get bucket information including timestamps in ISO 8601 UTC with Z suffix.",
)
async def get_bucket(
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Retrieve information about a specific bucket (owner/member/admin only)."""
    check_bucket_access(bucket, ctx)

    bucket_dir = _bucket_path(bucket)
    bucket_stat = Path(bucket_dir).stat()
    times = {
        "adate": bucket_stat.st_atime,
        "cdate": bucket_stat.st_ctime,
        "mdate": bucket_stat.st_mtime,
    }
    formatted_times = {key: to_iso(value) for key, value in times.items()}

    return JSONResponse(status_code=status.HTTP_200_OK, content={"name": bucket, **formatted_times})


@auth_router.post("/api/v1/buckets")
async def create_bucket(
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Create a new bucket. The creator becomes the owner.

    Returns 409 if the bucket name is already taken (directory exists).
    """
    try:
        _create_bucket(bucket, owner_account_id=ctx.id, owner_name=ctx.name)
    except ConflictError:
        return JSONResponse(
            status_code=409,
            content={
                "info": "Bucket already exists",
                "bucket": bucket,
            },
        )

    return JSONResponse(
        status_code=201,
        content={
            "info": "Bucket created successfully",
            "bucket": bucket,
        },
    )


@auth_router.delete("/api/v1/buckets/{bucket}")
async def delete_bucket(
    bucket: str,
    ctx: Annotated[AuthContext, Depends(require_admin)],
) -> JSONResponse:
    """Delete a bucket and all its contents (admin only).

    Cleans up: filesystem directory, DB rows (membership, versions,
    tombstones, bucket mapping).  Non-admin callers get 403; non-existent
    or inaccessible buckets return 404.
    """
    _delete_bucket(bucket)
    log.info(f'Bucket: "{bucket}" deleted by admin {ctx.name}')

    return JSONResponse(
        status_code=200,
        content={
            "info": "Bucket deleted successfully",
            "bucket": bucket,
        },
    )


log.info(f"Module {__name__} loaded")
