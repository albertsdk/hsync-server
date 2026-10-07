# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the ADR-0023 corrected-mtime tie-break in upload arbitration."""

from unittest.mock import patch

from hsync_common.models.version import SyncStateStore

from hsync_server.arbitration import ArbitrationOutcome, arbitrate_upload

CID_A = "TestClientA1234567890"
CID_B = "TestClientB1234567890"


def _store_with(relpath: str, base_hash: str, vclock: dict, corrected: int = 0) -> SyncStateStore:
    """Return an in-memory store preloaded with one recorded version."""
    store = SyncStateStore()
    store.record(
        relpath,
        base_hash=base_hash,
        vclock=vclock,
        version_id="v1",
        origin_cid=CID_B,
        corrected_mtime=corrected,
    )
    return store


def _arbitrate(
    relpath: str,
    incoming_hash: str,
    incoming_vclock: dict,
    stored: SyncStateStore,
    *,
    incoming_corrected: int = 0,
    disk_hash: str | None,
):
    """Drive arbitrate_upload with an in-memory store (no filesystem).

    ``disk_hash`` must match the stored base (or be ``None`` when no file is
    on disk) so the out-of-band self-record pre-step never fires: these tests
    exercise only the tie-break.
    """
    with (
        patch("hsync_server.arbitration.get_version_store", return_value=stored),
        patch("hsync_server.arbitration.get_server_cid", return_value="TestClientX1234567890"),
    ):
        return arbitrate_upload(
            bucket="b1",
            relpath=relpath,
            incoming_hash=incoming_hash,
            incoming_vclock_raw=incoming_vclock,
            incoming_version_id="v-in",
            incoming_origin_cid=CID_A,
            disk_hash=disk_hash,
            incoming_corrected_mtime=incoming_corrected,
        )


class TestArbitrationCorrectedMtime:
    def test_incoming_newer_corrected_mtime_wins_tie_break(self):
        """Concurrent clocks + newer incoming corrected mtime ⇒ accepted_conflict_won."""
        stored = _store_with("f.txt", "h-stored", {CID_B: 1}, corrected=100)
        result = _arbitrate(
            "f.txt",
            "h-incoming",
            {CID_A: 1},
            stored,
            incoming_corrected=200,
            disk_hash="h-stored",
        )
        assert result.outcome is ArbitrationOutcome.accepted_conflict_won
        assert result.record_corrected_mtime == 200

    def test_stored_newer_corrected_mtime_wins_tie_break(self):
        """Concurrent clocks + newer stored corrected mtime ⇒ conflict_lost."""
        stored = _store_with("f.txt", "h-stored", {CID_B: 1}, corrected=200)
        result = _arbitrate(
            "f.txt",
            "h-incoming",
            {CID_A: 1},
            stored,
            incoming_corrected=100,
            disk_hash="h-stored",
        )
        assert result.outcome is ArbitrationOutcome.conflict_lost
        assert result.record_corrected_mtime == 200

    def test_missing_corrected_mtime_falls_back_to_hash(self):
        """A side with corrected mtime 0 degrades to the deterministic hash fallback."""
        stored = _store_with("f.txt", "aaa", {CID_B: 1}, corrected=0)
        result = _arbitrate(
            "f.txt",
            "zzz",
            {CID_A: 1},
            stored,
            incoming_corrected=100,
            disk_hash="aaa",
        )
        # "zzz" > "aaa" ⇒ incoming wins the hash fallback.
        assert result.outcome is ArbitrationOutcome.accepted_conflict_won

    def test_dominance_never_overridden_by_corrected_mtime(self):
        """Causal dominance wins regardless of the corrected mtimes."""
        stored = _store_with("f.txt", "h-stored", {CID_B: 2}, corrected=999)
        result = _arbitrate(
            "f.txt",
            "h-incoming",
            {CID_B: 1},  # dominated by {B:2}
            stored,
            incoming_corrected=1,
            disk_hash="h-stored",
        )
        # Incoming is an ancestor: superseded, never a tie-break.
        assert result.outcome is ArbitrationOutcome.superseded

    def test_accepted_dominance_records_incoming_corrected_mtime(self):
        """An accepted causally-ahead upload records the incoming corrected mtime."""
        stored = _store_with("f.txt", "h-stored", {CID_B: 1}, corrected=50)
        result = _arbitrate(
            "f.txt",
            "h-incoming",
            {CID_B: 2},  # dominates {B:1}
            stored,
            incoming_corrected=120,
            disk_hash="h-stored",
        )
        assert result.outcome is ArbitrationOutcome.accepted
        assert result.record_corrected_mtime == 120


def test_negative_corrected_mtime_behaves_as_unknown():
    """A negative corrected mtime never participates in the tie-break (the
    matrix requires both values > 0), so the deterministic hash fallback
    decides: "zzz" > "aaa" ⇒ incoming wins."""
    stored = _store_with("f.txt", "aaa", {CID_B: 1}, corrected=0)
    result = _arbitrate(
        "f.txt",
        "zzz",
        {CID_A: 1},
        stored,
        incoming_corrected=-5,
        disk_hash="aaa",
    )
    assert result.outcome is ArbitrationOutcome.accepted_conflict_won
