# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server version-store micro-benchmark (temp bucket + state dir).

The server advertises a causal ``VersionInfo`` for every file it lists and
records one for every file it accepts. This measures the server binding around
the shared :class:`~hsync_common.models.version.SyncStateStore`:

* ``server_version_info(recorded)`` — the per-listing-entry hot path: bucket
  rel-path resolution + store read + ``VersionInfo`` build (`versions.py:145`);
* ``server_version_info(missing)`` — the unrecorded-path branch (`None`);
* ``record_version`` — the per-upload record + SQLite ``save()`` (`versions.py:74`),
  sampled at a lower count because it pays a real persistence round-trip.

Measured ops assert the returned/recorded version so a dropped or phantom entry
cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_versions.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    isolate_server_state,
    make_cid,
    maybe_write_json,
    measure,
    print_table,
)

# Point the version store (syncstate.db) + buckets root at a temp dir.
isolate_server_state()

from hsync_server.config import get_buckets_path  # noqa: E402
from hsync_server.versions import (  # noqa: E402
    record_version,
    reset_cache,
    server_version_info,
)

_BASE = "a" * 64


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    bucket = "bench-bucket"
    cid = make_cid(1)
    bucket_dir = Path(get_buckets_path()) / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    abs_path = str(bucket_dir / "dir" / "file.txt")
    missing_path = str(bucket_dir / "nope.txt")
    Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
    Path(abs_path).write_bytes(b"x")

    reset_cache()
    record_version(
        bucket,
        str(bucket_dir),
        abs_path,
        base_hash=_BASE,
        version_id="v",
        origin_cid=cid,
        vclock={cid: 1},
    )
    info = server_version_info(bucket, str(bucket_dir), abs_path)
    assert info is not None and info["vclock"] == {cid: 1}
    assert server_version_info(bucket, str(bucket_dir), missing_path) is None

    def op_info_recorded() -> None:
        out = server_version_info(bucket, str(bucket_dir), abs_path)
        assert out is not None and out["vclock"] == {cid: 1}

    def op_info_missing() -> None:
        assert server_version_info(bucket, str(bucket_dir), missing_path) is None

    def op_record() -> None:
        record_version(
            bucket,
            str(bucket_dir),
            abs_path,
            base_hash=_BASE,
            version_id="v",
            origin_cid=cid,
            vclock={cid: 1},
        )

    results: list[BenchResult] = [
        measure("server_version_info(recorded)", op_info_recorded, count=count),
        measure("server_version_info(missing)", op_info_missing, count=count),
        measure("record_version(persist)", op_record, count=50),
    ]

    print(f"version-store micro-benchmark (ops={count}, temp bucket + state dir)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
