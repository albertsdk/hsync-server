# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — server file-utils micro-benchmark (temp file, no state dir).

Per-delete and per-upload helpers in :mod:`hsync_server.api.file_utils`:

* ``is_sentinel_file`` — the ``.hsynckeep`` sentinel check run for every scanned
  entry (`api/file_utils.py:27`);
* ``update_file_timestamp`` — the mtime restore after a write, including the
  far-future clamp that neutralises conflict engineering (`:110`).

The timestamp scenarios touch a temp file (a real ``utime``), so their cost is
the honest per-upload restore; the missing-mtime and sentinel scenarios are pure.

Measured ops assert the branch (sentinel true/false, no-op on missing mtime) so
a silently skipped restore cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_file_utils.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    print_table,
)

from hsync_server.api.file_utils import is_sentinel_file, update_file_timestamp  # noqa: E402

_NORMAL_MTIME = "1700000000"
_FAR_FUTURE_MTIME = "99999999999"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    # The clamp branch logs one warning per sample; silence the sink (the log
    # call itself stays in the measurement, mirroring the reconcile bench).
    from loguru import logger as log

    log.remove()

    with tempfile.TemporaryDirectory(prefix="hsync_fileutils_") as tmp:
        path = str(Path(tmp) / "file.txt")
        Path(path).write_bytes(b"x")

        assert is_sentinel_file(str(Path(tmp) / ".hsynckeep")) is True
        assert is_sentinel_file(path) is False
        update_file_timestamp(path, mtime=_NORMAL_MTIME)
        update_file_timestamp(path, mtime=_FAR_FUTURE_MTIME)
        update_file_timestamp(path, mtime=None)  # no-op path

        def op_is_sentinel() -> None:
            assert is_sentinel_file(str(Path(tmp) / ".hsynckeep")) is True

        def op_is_not_sentinel() -> None:
            assert is_sentinel_file(path) is False

        def op_timestamp_normal() -> None:
            update_file_timestamp(path, mtime=_NORMAL_MTIME)

        def op_timestamp_clamp() -> None:
            update_file_timestamp(path, mtime=_FAR_FUTURE_MTIME)

        def op_timestamp_missing() -> None:
            update_file_timestamp(path, mtime=None)

        results: list[BenchResult] = [
            measure("is_sentinel_file(hit)", op_is_sentinel, count=count),
            measure("is_sentinel_file(miss)", op_is_not_sentinel, count=count),
            measure("update_file_timestamp(normal)", op_timestamp_normal, count=count),
            measure("update_file_timestamp(clamp)", op_timestamp_clamp, count=count),
            measure("update_file_timestamp(missing)", op_timestamp_missing, count=count),
        ]

        print(f"file-utils micro-benchmark (ops={count}, temp file, real utime)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
