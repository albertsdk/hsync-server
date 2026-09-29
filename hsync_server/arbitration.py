# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. or a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Causal upload arbitration shared by the REST upload and delta-apply endpoints.

The server (or LAN master, mirroring this logic) is a transport hub, not an
authority: every upload is arbitrated causally against the current stored
version for the path (ADR-0022). The arbitration:

1. **Self-records out-of-band edits**: if the file on disk differs from the
   stored base_hash (a third-party write the server did not observe), the
   stored version is advanced with the server's reserved cid before comparing,
   so the receiver's clock is always honest.
2. **Compares** the incoming version against the stored version using
   :func:`resolve_three_way`.
3. **Decides**: accept (dominance/accepted_conflict_won), reject as stale
   (``superseded``), or reject as tie-break loser (``conflict_lost`` — the
   incoming bytes are preserved as a conflicted copy).

The critical section (read store → decide → write file → record) is serialized
per path by the caller via :class:`hsync_server.file_lock_manager.FileLockManager`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hsync_common.models.version import (
    SyncDecision,
    merge_vclocks,
    new_version_id,
    parse_version_info,
    resolve_three_way,
    tick_vclock,
)

from hsync_server.versions import get_server_cid, get_version_store


class ArbitrationOutcome(StrEnum):
    """Outcome of arbitrating an incoming upload against the stored version."""

    accepted = "accepted"  # incoming is causally ahead or at parity: write it
    accepted_conflict_won = "accepted_conflict_won"  # concurrent: incoming wins tie-break
    superseded = "superseded"  # incoming is causally dominated (stale): reject, no copy
    conflict_lost = "conflict_lost"  # concurrent: incoming loses tie-break: preserve it


@dataclass(frozen=True)
class ArbitrationResult:
    """Result of causal arbitration for an upload.

    ``record_vclock`` is the clock the receiver should record on accept
    (``merge_vclocks(incoming_vclock, stored_vclock)`` per the convergence
    invariant, ADR-0022 §5).
    ``record_corrected_mtime`` is the corrected mtime (ADR-0023) the receiver
    records alongside: the incoming value on accept, the stored value on
    reject, 0 when unknown.
    ``loser_cid`` is the author of the losing content for conflict-copy naming.
    """

    outcome: ArbitrationOutcome
    record_vclock: dict[str, int]
    record_corrected_mtime: int = 0
    loser_cid: str | None = None


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
    if stored is not None and disk_hash is not None and stored["base_hash"] != disk_hash:
        advanced_clock = tick_vclock(stored["vclock"], server_cid)
        store.record(
            relpath,
            base_hash=disk_hash,
            vclock=advanced_clock,
            version_id=new_version_id(),
            origin_cid=server_cid,
        )
        # Persist immediately: the self-record must survive a restart and a
        # reset_cache(), so listings/events keep advertising the advanced
        # version (the in-memory record alone would be lost).
        store.save()
        stored = store.get(relpath)

    stored_hash = stored["base_hash"] if stored else None
    stored_vclock = stored["vclock"] if stored else None

    # Parse the incoming wire vclock tolerantly (malformed ⇒ empty clock).
    incoming_info = parse_version_info(
        {
            "vclock": incoming_vclock_raw,
            "version_id": incoming_version_id,
            "origin_cid": incoming_origin_cid,
            "ts": 0,
        }
    )
    incoming_vclock = incoming_info["vclock"] if incoming_info else {}
    effective_origin = incoming_info["origin_cid"] if incoming_info else incoming_origin_cid

    stored_corrected_mtime = int(stored.get("corrected_mtime", 0) or 0) if stored else 0
    decision = resolve_three_way(
        incoming_hash,
        incoming_vclock,
        stored_hash,
        stored_vclock,
        None,  # no base_hash: the server compares incoming vs stored directly
        # (the base-identity fast paths are a client-side concern; the server
        # has no independent knowledge of the common ancestor beyond the
        # stored version, which is already one of the two sides here).
        local_corrected_mtime=incoming_corrected_mtime,
        remote_corrected_mtime=stored_corrected_mtime,
    )

    match decision:
        case SyncDecision.in_sync:
            # Identical content already on disk: no-op accept (caller may 409 dup).
            return ArbitrationResult(
                outcome=ArbitrationOutcome.accepted,
                record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
                record_corrected_mtime=incoming_corrected_mtime or stored_corrected_mtime,
            )
        case SyncDecision.upload:
            # Incoming causally ahead (or stored at base): accept.
            return ArbitrationResult(
                outcome=ArbitrationOutcome.accepted,
                record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
                record_corrected_mtime=incoming_corrected_mtime,
            )
        case SyncDecision.download:
            # Stored dominates incoming: the upload is stale (an ancestor).
            return ArbitrationResult(
                outcome=ArbitrationOutcome.superseded,
                record_vclock=stored_vclock or {},
                record_corrected_mtime=stored_corrected_mtime,
                loser_cid=None,
            )
        case SyncDecision.conflict_upload:
            # Incoming wins the tie-break ⇒ accept, preserve the stored loser.
            loser_cid = (
                stored["origin_cid"] if stored and stored["origin_cid"] else effective_origin
            )
            return ArbitrationResult(
                outcome=ArbitrationOutcome.accepted_conflict_won,
                record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
                record_corrected_mtime=incoming_corrected_mtime,
                loser_cid=loser_cid,
            )
        case _:
            # conflict_download: incoming loses ⇒ reject, preserve the incoming loser.
            return ArbitrationResult(
                outcome=ArbitrationOutcome.conflict_lost,
                record_vclock=stored_vclock or {},
                record_corrected_mtime=stored_corrected_mtime,
                loser_cid=effective_origin,
            )


def conflicted_copy_name(file_location: str, cid: str | None, ts: str) -> str:
    """Build the canonical conflicted-copy path for a losing content.

    ``<stem>_conflicted_copy_<ts>_<cid>.<ext>`` (ADR-0021).
    """
    safe_cid = "".join(c for c in (cid or "unknown") if c.isalnum() or c in "-_.")[:32] or "unknown"
    path = Path(file_location)  # one Path parse instead of three
    stem = path.stem
    suffix = path.suffix
    parent = str(path.parent)
    return f"{parent}/{stem}_conflicted_copy_{ts}_{safe_cid}{suffix}"
