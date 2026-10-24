# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Causal upload arbitration shared by the REST upload and delta-apply endpoints.

The server (or LAN master, mirroring this logic) is a transport hub, not an
authority: every upload is arbitrated causally against the current stored
version for the path (ADR-0022):

1. **Self-records out-of-band edits**: if the file on disk differs from the
   stored base_hash (a third-party write the server did not observe), the
   stored version is advanced with the server's reserved cid before comparing,
   so the receiver's clock is always honest.
2. **Decides** via the shared reference implementation in
   :mod:`hsync_common.models.arbitration`: accept, reject as stale
   (``superseded``), or reject as tie-break loser (``conflict_lost`` — the
   incoming bytes are preserved as a conflicted copy).

The critical section (read store → decide → write file → record) is serialized
per path by the caller via :class:`hsync_server.file_lock_manager.FileLockManager`.
"""

from __future__ import annotations

from hsync_common.models.arbitration import (
    ArbitrationOutcome,
    ArbitrationResult,
    conflicted_copy_name,
    decide_arbitration,
    plan_self_record,
)
from hsync_common.models.version import new_version_id

from hsync_server.versions import get_server_cid, get_version_store

__all__ = [
    "ArbitrationOutcome",
    "ArbitrationResult",
    "arbitrate_upload",
    "conflicted_copy_name",
]


def arbitrate_upload(
    bucket: str,
    relpath: str,
    incoming_hash: str,
    incoming_vclock_raw: object,
    incoming_version_id: str,
    incoming_origin_cid: str | None,
    disk_hash: str | None,
    incoming_corrected_mtime: int = 0,
) -> ArbitrationResult:
    """Arbitrate an incoming upload against the stored version for *relpath*.

    Args:
        bucket: Bucket name (for the version store lookup).
        relpath: Bucket-relative path of the file.
        incoming_hash: SHA-256 of the incoming content.
        incoming_vclock_raw: Raw parsed ``vclock`` payload (validated here).
        incoming_version_id: Version id from the uploader.
        incoming_origin_cid: Origin cid from the uploader (may be None).
        disk_hash: SHA-256 of the current file on disk (None if no file).
        incoming_corrected_mtime: ADR-0023 corrected mtime advertised by the
            uploader (0 ⇒ unknown ⇒ the hash fallback applies).

    Returns:
        The :class:`ArbitrationResult`; the caller writes the file / copy /
        response based on ``outcome`` and records ``record_vclock`` on accept.
    """
    store = get_version_store(bucket)
    stored = store.get(relpath)

    # Self-record out-of-band edits: if the disk content differs from the
    # stored base, the server's view is stale — advance it with the server cid
    # before comparing (anti-aliasing invariant, ADR-0022 §3.4.7).
    server_cid = get_server_cid()
    advance = plan_self_record(stored, disk_hash, server_cid)
    if advance is not None:
        store.record(
            relpath,
            base_hash=advance.base_hash,
            vclock=advance.vclock,
            version_id=new_version_id(),
            origin_cid=server_cid,
        )
        # Persist immediately: the self-record must survive a restart and a
        # reset_cache(), so listings/events keep advertising the advanced
        # version (the in-memory record alone would be lost).
        store.save()
        stored = store.get(relpath)

    return decide_arbitration(
        stored=stored,
        incoming_hash=incoming_hash,
        incoming_vclock_raw=incoming_vclock_raw,
        incoming_version_id=incoming_version_id,
        incoming_origin_cid=incoming_origin_cid,
        incoming_corrected_mtime=incoming_corrected_mtime,
    )
