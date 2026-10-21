# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared post-publish bookkeeping for arbitrated file versions.

The full-upload and delta-apply endpoints materialise a new file version and
must then update the same derived state: the SHA-256 cache, deletion markers,
the causal version store and the metadata captured for the WebSocket
broadcast. Keeping that sequence in one place prevents the two copies from
drifting (in particular the cache invalidation order).
"""

from __future__ import annotations

import asyncio

from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache, is_hidden_file

from hsync_server.arbitration import ArbitrationResult
from hsync_server.hashes import set_sha256_hash
from hsync_server.tombstones import clear_deletion
from hsync_server.versions import record_version, server_version_info


async def record_published_version(
    *,
    bucket: str,
    local_bucket: str,
    file_location: str,
    base_hash: str,
    version_id: str | None,
    origin_cid: str | None,
    arbitration: ArbitrationResult,
) -> LocalFile | None:
    """Update derived state after ``file_location`` was atomically published.

    The caller must already have replaced the destination with verified
    content and applied its final timestamps/permissions. The SHA-256 cache is
    invalidated *before* being repopulated: its entry captures the published
    file's ``(mtime_ns, size)``, so it must be written once metadata is final.

    Returns the published file model for the caller to broadcast under its
    existing lock policy, or ``None`` for hidden/unreadable entries.
    """
    invalidate_sha256_cache(file_location)
    set_sha256_hash(file_location, base_hash)
    clear_deletion(bucket, local_bucket, file_location)
    record_version(
        bucket,
        local_bucket,
        file_location,
        base_hash=base_hash,
        vclock=arbitration.record_vclock,
        version_id=version_id,
        origin_cid=origin_cid,
        corrected_mtime=arbitration.record_corrected_mtime,
    )
    if is_hidden_file(file_location):
        return None
    src = await asyncio.to_thread(LocalFile.get, file_location)
    if src is not None:
        src.version = server_version_info(bucket, local_bucket, file_location)
    return src
