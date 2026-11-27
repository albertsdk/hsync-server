# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Retention policy for the server history store (ADR-0057).

The store keeps the bytes a write replaces or a delete removes, so without a
sweep it grows for as long as the deployment runs. This module owns the policy in
one place — the operator-facing defaults, the size budget derived from the volume
that hosts the store, and the periodic sweep the lifespan starts — so the admin
CLI, the REST surface and the background task cannot prune under different rules.

The sweep is idempotent and race-safe by construction (``history.prune`` removes
directories with ``ignore_errors`` and skips an unreadable record): running one
per uvicorn worker costs a duplicated walk, never a corrupted store.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from hsync_common.defaults import (
    HISTORY_QUOTA_FLOOR_MB_DEFAULT,
    HISTORY_QUOTA_PERCENT_DEFAULT,
    HISTORY_RETENTION_DAYS_DEFAULT,
)
from hsync_common.disk import share_of_volume_bytes
from loguru import logger as log

from hsync_server import history
from hsync_server.config import get_state_dir

__all__ = [
    "SWEEP_INTERVAL_S",
    "PruneBounds",
    "default_bounds",
    "prune_now",
    "run_periodic_prune",
]

# Retention is expressed in days, so one pass a day holds the store within about
# that much of its window; walking it more often would only add I/O.
SWEEP_INTERVAL_S = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class PruneBounds:
    """One prune's bounds: the age window first, the byte budget second.

    ``max_bytes=None`` keeps the age bound alone (the CLI's ``--max-size 0``).
    """

    retention_days: int
    max_bytes: int | None


def default_bounds() -> PruneBounds:
    """Return the deployment policy: the shared defaults, sized from the volume.

    Consumers: ``hsync-server`` (admin CLI, ``api/admin.py``, the sweep).
    """
    return PruneBounds(
        retention_days=HISTORY_RETENTION_DAYS_DEFAULT,
        max_bytes=share_of_volume_bytes(
            Path(get_state_dir()),
            percent=HISTORY_QUOTA_PERCENT_DEFAULT,
            floor_mb=HISTORY_QUOTA_FLOOR_MB_DEFAULT,
        ),
    )


def prune_now(
    bounds: PruneBounds | None = None,
    *,
    bucket: str | None = None,
    dry_run: bool = False,
) -> history.PruneReport:
    """Apply *bounds* (default: :func:`default_bounds`) and report what was freed.

    Consumers: ``hsync-server`` (admin CLI, the sweep).
    """
    applied = default_bounds() if bounds is None else bounds
    report = history.prune(
        bucket=bucket,
        retention_days=applied.retention_days,
        max_bytes=applied.max_bytes,
        dry_run=dry_run,
    )
    if report.removed and not dry_run:
        log.info(
            f"History retention: removed {report.removed} preserved version(s), "
            f"{report.freed} B (retention {applied.retention_days}d)"
        )
    return report


async def run_periodic_prune(stop: asyncio.Event, *, interval_s: float = SWEEP_INTERVAL_S) -> None:
    """Sweep the store now, then every *interval_s*, until *stop* is set.

    The first pass runs immediately: a deployment that was offline for longer
    than the retention window would otherwise stay oversized for a whole day.
    The prune runs in a worker thread, and a failure is logged instead of
    ending the loop — retention must never take the server down with it.
    """
    while True:
        try:
            await asyncio.to_thread(prune_now)
        except Exception as exc:
            log.warning(f"History retention sweep failed: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            continue
