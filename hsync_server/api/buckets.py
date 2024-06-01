# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Bucket operations API for HSYNC Server.

This module provides REST API endpoints for bucket management:
- Get bucket information
- Create new buckets
- Delete buckets

All endpoints require API key authentication via Bearer token.
"""

from datetime import UTC, datetime
from os import makedirs
from os.path import exists, getatime, getctime, getmtime
from shutil import rmtree

from fastapi import status
from fastapi.responses import JSONResponse
from loguru import logger as log

from hsync_server.api.utils import is_valid_bucket
from hsync_server.config import HSYNC_BUCKETS
from hsync_server.exceptions import (
    BucketNotFoundError,
    DeleteError,
    InvalidBucketNameError,
)
from hsync_server.routers import auth_router


@auth_router.get(
    "/api/v1/buckets/{bucket}",
    name="Get bucket information",
    summary="Get bucket information",
    description="""Get bucket information including timestamps.""",
)
async def get_bucket(bucket: str) -> JSONResponse:
    """
    Retrieve information about a specific bucket.

    Returns the bucket name and timestamps (access, creation, modification)
    in ISO format.

    Args:
        bucket: Name of the bucket to query.

    Returns:
        JSON response with bucket name and timestamps.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

    if not exists(bucket_dir):
        raise BucketNotFoundError(details={"bucket": bucket})

    # Fetch and format access, creation and modification times
    times = {
        "adate": getatime(bucket_dir),
        "cdate": getctime(bucket_dir),
        "mdate": getmtime(bucket_dir),
    }
    formatted_times = {
        key: datetime.fromtimestamp(value, tz=UTC).isoformat() for key, value in times.items()
    }

    return JSONResponse(status_code=status.HTTP_200_OK, content={"name": bucket, **formatted_times})


@auth_router.post("/api/v1/buckets")
async def create_bucket(bucket: str) -> JSONResponse:
    """
    Create a new storage bucket.

    Creates a directory in the buckets storage area with the specified name.
    Bucket names must be alphanumeric (hyphens and underscores allowed,
    but not at the beginning or end).

    Args:
        bucket: Name for the new bucket.

    Returns:
        JSON response confirming creation or conflict if exists.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_directory = f"{HSYNC_BUCKETS}/{bucket}"

    # Check if bucket already exists
    if exists(bucket_directory):
        log.warning(f"Bucket already exists: {bucket_directory}")
        return JSONResponse(
            status_code=409,
            content={
                "info": "Bucket already exists",
                "bucket": bucket,
            },
        )

    makedirs(bucket_directory, exist_ok=True)
    log.info(f'Bucket: "{bucket}" created')

    return JSONResponse(
        status_code=201,
        content={
            "info": "Bucket created successfully",
            "bucket": bucket,
        },
    )


@auth_router.delete("/api/v1/buckets/{bucket}")
async def delete_bucket(bucket: str) -> JSONResponse:
    """
    Delete a bucket and all its contents.

    Permanently removes the bucket directory and all files/subdirectories.
    This operation cannot be undone.

    Args:
        bucket: Name of the bucket to delete.

    Returns:
        JSON response confirming deletion.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
        DeleteError: If deletion fails.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

    if not exists(bucket_dir):
        raise BucketNotFoundError(details={"bucket": bucket})

    try:
        # Remove bucket and all contents
        rmtree(bucket_dir)
        log.info(f'Bucket: "{bucket}" deleted successfully')

        return JSONResponse(
            status_code=200,
            content={
                "info": "Bucket deleted successfully",
                "bucket": bucket,
            },
        )
    except OSError as e:
        log.error(f"Failed to delete bucket {bucket}: {e}", exc_info=True)
        raise DeleteError(
            message=f"Failed to delete bucket: {bucket}",
            details={"bucket": bucket, "error": str(e)},
        )


log.info(f"Module {__name__} loaded")
