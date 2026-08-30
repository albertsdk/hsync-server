# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server API-utils micro-benchmark (per-request bucket confinement).

Every REST file endpoint resolves a client-supplied relative path inside a
*specific* bucket before touching the filesystem. ``resolve_within_bucket``
(``api/utils.py:50``) is the cross-bucket confinement gate: a path such as
``../other-bucket/x`` can never escape into a sibling bucket. This benchmark
measures that wrapper plus bucket-name validation:

* ``resolve_within_bucket(hit)`` — the confined-resolve of an in-bucket path;
* ``resolve_within_bucket(escape)`` — traversal rejected (shared ``validate_remote_relpath``);
* ``resolve_within_bucket(no-final-deref)`` — the symlink-preserving branch;
* ``is_valid_bucket`` — bucket-name gate (``api/utils.py:34``).

Measured ops assert confinement/rejection so a path that escapes or a name that
slips through cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_utils.py [--count N] [--json-out FILE]
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
    maybe_write_json,
    measure,
    print_table,
)

# Point the bucket root at a temp dir (get_settings() is cached), then import.
isolate_server_state()

from hsync_server.api.utils import is_valid_bucket, resolve_within_bucket  # noqa: E402
from hsync_server.config import get_buckets_path  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    bucket_dir = Path(get_buckets_path()) / "mybucket"
    (bucket_dir / "a").mkdir(parents=True, exist_ok=True)
    (bucket_dir / "a" / "b.txt").write_bytes(b"x")

    hit = resolve_within_bucket("mybucket", "a/b.txt")
    escaped = resolve_within_bucket("mybucket", "../evil.txt")
    no_deref = resolve_within_bucket("mybucket", "a/b.txt", resolve_final=False)
    assert hit is not None and hit.endswith("a/b.txt")
    assert escaped is None
    assert no_deref is not None
    assert is_valid_bucket("my-bucket_1") is True
    assert is_valid_bucket("../bad") is False

    def op_resolve_hit() -> None:
        out = resolve_within_bucket("mybucket", "a/b.txt")
        assert out is not None and out.endswith("a/b.txt")

    def op_resolve_escape() -> None:
        assert resolve_within_bucket("mybucket", "../evil.txt") is None

    def op_resolve_no_deref() -> None:
        out = resolve_within_bucket("mybucket", "a/b.txt", resolve_final=False)
        assert out is not None

    def op_is_valid_bucket() -> None:
        assert is_valid_bucket("my-bucket_1") is True

    results: list[BenchResult] = [
        measure("resolve_within_bucket(hit)", op_resolve_hit, count=count),
        measure("resolve_within_bucket(escape)", op_resolve_escape, count=count),
        measure("resolve_within_bucket(no-deref)", op_resolve_no_deref, count=count),
        measure("is_valid_bucket", op_is_valid_bucket, count=count),
    ]

    print(f"api-utils micro-benchmark (ops={count}, confinement + validation)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
