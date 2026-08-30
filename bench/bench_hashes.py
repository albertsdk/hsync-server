# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server hash-cache micro-benchmark (temp file + seeded FILE_HASHES).

The server serves a SHA-256 digest per file on list/upload/download. Its cache
maps ``SHA-256(filepath)`` → ``(content_sha256, mtime_ns, size)`` and refuses a
stale entry whose current stat no longer matches (``hashes.py``). This measures
the per-file cache wrapper, not the digest computation itself (that is the
``hsync-common`` hashing path, already benchmarked there):

* ``set_sha256_hash`` — SHA-256(key) + ``os.stat`` + dict store;
* ``get_sha256_hash(hit)`` — the freshness check (stat + compare) that runs on
  every cache hit;
* ``get_sha256_hash(miss)`` — key hash + dict miss (no stat);
* ``del_sha256_hash(hit/miss)`` — present/absent deletion.

Measured ops assert the served/absent digest so a wrongly-stale or wrongly-hit
cache cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_hashes.py [--count N] [--json-out FILE]
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

from hsync_server.hashes import (  # noqa: E402
    FILE_HASHES,
    del_sha256_hash,
    get_sha256_hash,
    set_sha256_hash,
)

_SHA = "a" * 64


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    with tempfile.TemporaryDirectory(prefix="hsync_hashes_") as tmp:
        hit_path = str(Path(tmp) / "hit.txt")
        miss_path = str(Path(tmp) / "miss.txt")
        Path(hit_path).write_bytes(b"x" * 64)

        FILE_HASHES.clear()
        set_sha256_hash(hit_path, _SHA)
        assert get_sha256_hash(hit_path) == _SHA
        assert get_sha256_hash(miss_path) is None
        assert del_sha256_hash(miss_path) is False

        def op_set() -> None:
            set_sha256_hash(hit_path, _SHA)
            assert FILE_HASHES  # the entry is (re)stored, never silently dropped

        def op_get_hit() -> None:
            assert get_sha256_hash(hit_path) == _SHA

        def op_get_miss() -> None:
            assert get_sha256_hash(miss_path) is None

        def op_del_hit() -> None:
            set_sha256_hash(hit_path, _SHA)
            assert del_sha256_hash(hit_path) is True

        def op_del_miss() -> None:
            assert del_sha256_hash(miss_path) is False

        results: list[BenchResult] = [
            measure("set_sha256_hash", op_set, count=count),
            measure("get_sha256_hash(hit)", op_get_hit, count=count),
            measure("get_sha256_hash(miss)", op_get_miss, count=count),
            measure("del_sha256_hash(hit)", op_del_hit, count=count),
            measure("del_sha256_hash(miss)", op_del_miss, count=count),
        ]

        print(f"hash-cache micro-benchmark (ops={count}, temp file + seeded cache)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
