# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Helpers shared by the tests of the server history store (ADR-0057)."""

from __future__ import annotations

import json
from pathlib import Path

from hsync_common.constants import HISTORY_DIRNAME, HISTORY_ENTRY_FILENAME


def age_entry(state_dir: Path, bucket: str, entry_id: str, recorded_at: int) -> None:
    """Rewrite an entry's timestamp, the way only the clock can in production."""
    record = state_dir / HISTORY_DIRNAME / bucket / entry_id / HISTORY_ENTRY_FILENAME
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["recorded_at"] = recorded_at
    record.write_text(json.dumps(payload), encoding="utf-8")
