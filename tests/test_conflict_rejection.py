# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the shared conflict-rejection plan (upload + delta apply)."""

from __future__ import annotations

from pathlib import Path

from hsync_common.models.arbitration import ArbitrationOutcome, ArbitrationResult

from hsync_server.api.files._conflicts import plan_conflict_rejection
from hsync_server.exceptions import ConflictLostError, SupersededError


def _result(outcome: ArbitrationOutcome, *, loser: str | None = None) -> ArbitrationResult:
    return ArbitrationResult(outcome=outcome, record_vclock={}, loser_cid=loser)


class TestPlanConflictRejection:
    def test_accepted_outcomes_need_no_rejection(self):
        for outcome in (ArbitrationOutcome.accepted, ArbitrationOutcome.accepted_conflict_won):
            assert (
                plan_conflict_rejection(
                    _result(outcome),
                    kind="Upload",
                    final_path="/b/f.txt",
                    sha_prefix="abcd",
                )
                is None
            )

    def test_superseded_discards_without_a_conflicted_copy(self):
        rejection = plan_conflict_rejection(
            _result(ArbitrationOutcome.superseded),
            kind="Upload",
            final_path="/b/f.txt",
            sha_prefix="abcd",
            has_backup=True,
        )
        assert rejection is not None
        assert isinstance(rejection.error, SupersededError)
        assert rejection.error.message == "Upload superseded by a newer version on the server"
        assert rejection.error.details == {"sha256sum": "abcd"}
        assert rejection.conflicted_path is None
        assert rejection.discard_backup is True

    def test_conflict_lost_preserves_the_canonical_copy(self):
        rejection = plan_conflict_rejection(
            _result(ArbitrationOutcome.conflict_lost, loser="PeerA"),
            kind="Delta upload",
            final_path="/b/f.txt",
            sha_prefix="abcd",
        )
        assert rejection is not None
        assert isinstance(rejection.error, ConflictLostError)
        assert rejection.conflicted_path is not None
        name = Path(rejection.conflicted_path).name
        assert name.startswith("f_conflicted_copy_")
        assert name.endswith("_PeerA.txt")
        assert rejection.error.details == {"sha256sum": "abcd", "conflicted_copy": name}
        assert "Delta upload lost a conflict tie-break" in rejection.error.message
        assert rejection.discard_backup is False

    def test_conflicted_copy_uses_the_final_path_not_the_part_artifact(self):
        """A `.part`/`.delta.part` suffix must never leak into the synced tree."""
        rejection = plan_conflict_rejection(
            _result(ArbitrationOutcome.conflict_lost, loser="PeerB"),
            kind="Delta upload",
            final_path="/b/report.pdf",
            sha_prefix="abcd",
        )
        assert rejection is not None
        assert rejection.conflicted_path is not None
        name = Path(rejection.conflicted_path).name
        assert name.startswith("report_conflicted_copy_")
        assert name.endswith("_PeerB.pdf")
        assert ".part" not in name
