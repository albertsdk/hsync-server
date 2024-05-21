# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File download endpoint for HSYNC Server.

This module provides the REST API endpoint for file download with:
- Streaming response
- Optional zstd compression
- Range request support (for resumable downloads)
"""

from aiofiles.ospath import isfile
from fastapi import Request
from fastapi.responses import FileResponse, Response, JSONResponse
from loguru import logger as log
from os.path import exists, getsize

from hsync_server.api.utils import is_valid_bucket, is_valid_dir
from hsync_server.compression import compress_data, should_compress, CONTENT_ENCODING_ZSTD
from hsync_server.config import HSYNC_BUCKETS, get_compression_enabled
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
    InvalidBucketNameError,
    InvalidPathError,
)
from hsync_server.rate_limiter import limiter, RATE_LIMIT_DOWNLOAD
from hsync_server.routers import auth_router


@auth_router.get("/api/v1/buckets/{bucket}/files/download")
@limiter.limit(RATE_LIMIT_DOWNLOAD)
async def download_file(
    request: Request, bucket: str, filename: str
):
    """
    Download a file from a bucket with optional zstd compression.

    Streams the file content as a response with appropriate headers.
    If compression is enabled and beneficial, compresses the file before sending.

    Args:
        request: The incoming FastAPI request.
        bucket: Name of the bucket containing the file.
        filename: Path to the file within the bucket.

    Returns:
        FileResponse or Response with the file content for download.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        HSyncFileNotFoundError: If file does not exist.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if not is_valid_dir(filename):
        raise InvalidPathError(details={"path": filename})

    file = f"{HSYNC_BUCKETS}/{bucket}/{filename}"

    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    file_size = getsize(file)

    # Check if compression should be used:
    # - Compression is enabled on server
    # - File is compressible (not too small, not pre-compressed)
    # - No Range header (resume requests skip compression)
    has_range_header = "range" in request.headers
    use_compression = (
        get_compression_enabled()
        and should_compress(filename, file_size)
        and not has_range_header
    )

    if use_compression:
        # Read and compress file
        compressed_data = compress_data(open(file, "rb").read())
        compression_ratio = len(compressed_data) / file_size * 100
        log.debug(
            f"Compressed download: {file_size} -> {len(compressed_data)} bytes "
            f"({compression_ratio:.1f}%)"
        )
        return Response(
            content=compressed_data,
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": CONTENT_ENCODING_ZSTD,
                "Content-Disposition": f'attachment; filename="{filename.split("/")[-1]}"',
            },
        )

    # Standard download (streaming, supports Range requests)
    return FileResponse(file, filename=filename)
