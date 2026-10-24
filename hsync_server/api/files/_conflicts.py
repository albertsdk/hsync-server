# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Conflict rejections shared by the upload and delta-apply endpoints.

Both endpoints arbitrate an incoming version and must answer identically when it
loses: ``superseded`` discards the incoming bytes (the winner already holds
them), ``conflict_lost`` preserves them as the canonical conflicted copy. Only
the message's subject differs ("Upload" vs "Delta upload") and the artifact each
endpoint tracks (a backup copy vs the rebuilt ``.delta.part``), so the decision —
including the conflicted-copy name and the error payload — is built here while
the caller performs the move/removal with its own bookkeeping.

Consumers: ``hsync_server.api.files.upload``,
``hsync_server.api.files.delta.apply``, tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hsync_common.models.arbitration import (
    ArbitrationOutcome,
    ArbitrationResult,
    conflicted_copy_name,
)
from hsync_common.time_utils import conflict_timestamp

from hsync_server.exceptions import ConflictLostError, SupersededError

__all__ = ["ConflictRejection", "plan_conflict_rejection"]


@dataclass(frozen=True, slots=True)
class ConflictRejection:
    """Rejection to raise plus the filesystem action the caller must perform."""

    error: SupersededError | ConflictLostError
    conflicted_path: str | None  # move the incoming bytes here; None = discard
    discard_backup: bool  # the preserved previous copy is no longer needed


def plan_conflict_rejection(
    result: ArbitrationResult,
    *,
    kind: str,
    final_path: str,
    sha_prefix: str,
    has_backup: bool = False,
) -> ConflictRejection | None:
    """Describe the rejection a lost upload needs, or ``None`` when accepted.

    ``kind`` names the upload kind in the error message ("Upload" / "Delta
    upload"). The conflicted copy is named after ``final_path``, never the temp
    artifact, so a ``.part``/``.delta.part`` suffix cannot leak into the synced
    tree (ADR-0021).
    """
    if result.outcome is ArbitrationOutcome.superseded:
        return ConflictRejection(
            error=SupersededError(
                message=f"{kind} superseded by a newer version on the server",
                details={"sha256sum": sha_prefix},
            ),
            conflicted_path=None,
            discard_backup=has_backup,
        )
    if result.outcome is ArbitrationOutcome.conflict_lost:
        conflicted = conflicted_copy_name(final_path, result.loser_cid, conflict_timestamp())
        return ConflictRejection(
            error=ConflictLostError(
                message=f"{kind} lost a conflict tie-break; content preserved as conflicted copy",
                details={"sha256sum": sha_prefix, "conflicted_copy": Path(conflicted).name},
            ),
            conflicted_path=conflicted,
            discard_backup=has_backup,
        )
    return None
