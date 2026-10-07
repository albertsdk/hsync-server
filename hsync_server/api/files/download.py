# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File download endpoint for HSYNC Server.

This module provides the REST API endpoint for file download with:
- Streaming response
- Optional zstd compression
- Range request support (for resumable downloads)
"""

import asyncio
import stat as stat_module
from os import stat as os_stat
from pathlib import Path
from time import monotonic
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import compute_sha256_async
from hsync_common.protocol.rest import REST_FILES_DOWNLOAD_SUFFIX, rest_bucket_template
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.utils import build_content_disposition, resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import (
    CONTENT_ENCODING_ZSTD,
    async_compress_stream,
    should_compress,
)
from hsync_server.config import get_compression_enabled
from hsync_server.constants import BACKUP_SUFFIX, CONTENT_TYPE_OCTET_STREAM, ActionLabel
from hsync_server.exceptions import FileNotFoundError as HSyncFileNotFoundError
from hsync_server.exceptions import InvalidPathError
from hsync_server.hashes import get_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DOWNLOAD, limiter
from hsync_server.routers import auth_router


@auth_router.get(rest_bucket_template(REST_FILES_DOWNLOAD_SUFFIX))
@limiter.limit(RATE_LIMIT_DOWNLOAD)
async def download_file(
    request: Request,  # Required by rate limiter
    background_tasks: BackgroundTasks,
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    filename: str,
    cid: str = "",
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
    check_bucket_access(bucket, ctx)
    t0 = monotonic()
    # Confine to *this* bucket: the filename is untrusted input and must
    # never escape into a sibling bucket or outside the storage root. The
    # full resolve is intentional here: a symlink whose target stays inside
    # the bucket is served transparently (the standard static-server
    # semantics), while one whose target escapes the bucket never resolves
    # within the confinement and is rejected as InvalidPathError. Symlinks
    # are *replicated* structurally via the WS symlink event and the
    # /symlinks endpoint; this endpoint only serves content on demand.
    file = resolve_within_bucket(bucket, filename)
    if file is None:
        raise InvalidPathError(details={"path": filename})

    backup_file = f"{file}{BACKUP_SUFFIX}"

    # If a backup file exists (due to ongoing upload), serve that instead
    # This ensures clients always download a complete, valid file
    if Path(backup_file).exists():
        file = backup_file

    # Single stat in a worker thread replaces isfile() + stat() (two
    # filesystem lookups). os.path.isfile semantics are preserved exactly:
    # any OSError and any non-regular file map to the same 404.
    try:
        st = await asyncio.to_thread(os_stat, file)
    except OSError:
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})
    if not stat_module.S_ISREG(st.st_mode):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})
    file_size = st.st_size

    # Retrieve or compute the SHA-256 checksum of the file being served,
    # so the client can verify integrity against the current server-side state.
    sha256sum = get_sha256_hash(file)
    if sha256sum is None:
        sha256sum = await compute_sha256_async(file)

    checksum_header = {"X-Checksum-SHA256": sha256sum} if sha256sum else {}

    # - Compression is enabled on server
    # - File is compressible (not too small, not pre-compressed)
    # - No Range header (resume requests skip compression)
    has_range_header = "range" in request.headers
    use_compression = (
        get_compression_enabled() and should_compress(filename, file_size) and not has_range_header
    )

    # The download log is deferred to a background task so it can include the
    # actual transfer duration: the response body is streamed after this
    # function returns, so the wall-clock time is only known once the last
    # byte has been sent to the client.
    cid_suffix = f" (CID: {cid})" if cid else ""
    zstd_suffix = ", zstd" if use_compression else ""

    def _log_download_duration() -> None:
        log.info(
            f'{ActionLabel.DOWNLOAD} "{bucket}/{filename}"{cid_suffix} '
            f"{format_bytes(file_size)}{zstd_suffix} "
            f"{format_duration(monotonic() - t0, subsecond=True)}"
        )

    background_tasks.add_task(_log_download_duration)

    if use_compression:
        # Streaming compression: reads and compresses the file in chunks and
        # streams the single zstd frame to the client, so neither the file nor
        # its compressed form is ever fully buffered in memory (memory-bomb DoS).
        log.debug(f"Compressed streaming download: {file} ({file_size} bytes)")
        return StreamingResponse(
            async_compress_stream(file),
            media_type=CONTENT_TYPE_OCTET_STREAM,
            headers={
                "Content-Encoding": CONTENT_ENCODING_ZSTD,
                "Content-Disposition": build_content_disposition(filename.split("/")[-1]),
                **checksum_header,
            },
        )

    # Standard download (streaming, supports Range requests)
    return FileResponse(file, filename=filename, headers=checksum_header)
