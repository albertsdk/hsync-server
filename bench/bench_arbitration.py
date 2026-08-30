# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server upload-arbitration micro-benchmark (temp state dir).

Measures :func:`hsync_server.arbitration.arbitrate_upload` — the causal gate
the REST upload and delta-apply endpoints run on every incoming file before
touching the filesystem (ADR-0022, ADR-0023). The pure causal decision lives in
``hsync-common``; this benchmark measures the **server binding** around it: the
per-bucket version-store read, the reserved-node cid lookup, the tolerant
wire-version parse, and the ``ArbitrationResult`` assembly.

Four outcome branches are covered (one seeded store entry each): the untouched
first upload (``accepted``), a causally-dominant edit (``accepted``), a stale
re-push (``superseded``), a divergence won on the corrected-mtime tie-break
(``accepted_conflict_won``) and the losing divergence (``conflict_lost``).
``conflicted_copy_name`` (the ADR-0021 loser-path builder) is measured apart.

Measured ops assert the exact ``ArbitrationOutcome`` so a silently wrong verdict
cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_arbitration.py [--count N] [--json-out FILE]
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

# Point the version store (syncstate.db + .hsyncnodeid) at a temp state dir.
isolate_server_state()

from hsync_server.arbitration import (  # noqa: E402
    ArbitrationOutcome,
    arbitrate_upload,
    conflicted_copy_name,
)
from hsync_server.versions import get_version_store, reset_cache  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    bucket = "bench-bucket"
    cid_a = make_cid(1)
    cid_b = make_cid(2)
    h_first, h_old, h_new, h_w, h_l, h_in_w, h_in_l = (
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        "9" * 64,
    )

    reset_cache()
    store = get_version_store(bucket)
    store.record(
        "dominant.txt",
        base_hash=h_old,
        version_id="v",
        origin_cid=cid_a,
        vclock={cid_a: 2},
        corrected_mtime=10,
    )
    store.record(
        "superseded.txt",
        base_hash=h_new,
        version_id="v",
        origin_cid=cid_a,
        vclock={cid_a: 3},
        corrected_mtime=10,
    )
    store.record(
        "conflict_won.txt",
        base_hash=h_w,
        version_id="v",
        origin_cid=cid_a,
        vclock={cid_a: 2},
        corrected_mtime=10,
    )
    store.record(
        "conflict_lost.txt",
        base_hash=h_l,
        version_id="v",
        origin_cid=cid_a,
        vclock={cid_a: 2},
        corrected_mtime=20,
    )

    # Sanity: the seeds produce the expected verdicts.
    assert (
        arbitrate_upload(
            bucket, "first.txt", h_first, {cid_a: 1}, "v1", cid_a, disk_hash=None
        ).outcome
        is ArbitrationOutcome.accepted
    )
    assert (
        arbitrate_upload(
            bucket,
            "dominant.txt",
            h_new,
            {cid_a: 3},
            "v3",
            cid_a,
            disk_hash=h_old,
            incoming_corrected_mtime=20,
        ).outcome
        is ArbitrationOutcome.accepted
    )
    assert (
        arbitrate_upload(
            bucket,
            "superseded.txt",
            h_old,
            {cid_a: 1},
            "v1",
            cid_a,
            disk_hash=h_new,
            incoming_corrected_mtime=5,
        ).outcome
        is ArbitrationOutcome.superseded
    )
    assert (
        arbitrate_upload(
            bucket,
            "conflict_won.txt",
            h_in_w,
            {cid_b: 2},
            "v2",
            cid_b,
            disk_hash=h_w,
            incoming_corrected_mtime=20,
        ).outcome
        is ArbitrationOutcome.accepted_conflict_won
    )
    assert (
        arbitrate_upload(
            bucket,
            "conflict_lost.txt",
            h_in_l,
            {cid_b: 2},
            "v2",
            cid_b,
            disk_hash=h_l,
            incoming_corrected_mtime=5,
        ).outcome
        is ArbitrationOutcome.conflict_lost
    )

    def op_first() -> None:
        r = arbitrate_upload(bucket, "first.txt", h_first, {cid_a: 1}, "v1", cid_a, disk_hash=None)
        assert r.outcome is ArbitrationOutcome.accepted

    def op_dominant() -> None:
        r = arbitrate_upload(
            bucket,
            "dominant.txt",
            h_new,
            {cid_a: 3},
            "v3",
            cid_a,
            disk_hash=h_old,
            incoming_corrected_mtime=20,
        )
        assert r.outcome is ArbitrationOutcome.accepted

    def op_superseded() -> None:
        r = arbitrate_upload(
            bucket,
            "superseded.txt",
            h_old,
            {cid_a: 1},
            "v1",
            cid_a,
            disk_hash=h_new,
            incoming_corrected_mtime=5,
        )
        assert r.outcome is ArbitrationOutcome.superseded

    def op_conflict_won() -> None:
        r = arbitrate_upload(
            bucket,
            "conflict_won.txt",
            h_in_w,
            {cid_b: 2},
            "v2",
            cid_b,
            disk_hash=h_w,
            incoming_corrected_mtime=20,
        )
        assert r.outcome is ArbitrationOutcome.accepted_conflict_won

    def op_conflict_lost() -> None:
        r = arbitrate_upload(
            bucket,
            "conflict_lost.txt",
            h_in_l,
            {cid_b: 2},
            "v2",
            cid_b,
            disk_hash=h_l,
            incoming_corrected_mtime=5,
        )
        assert r.outcome is ArbitrationOutcome.conflict_lost

    def op_conflicted_name() -> None:
        name = conflicted_copy_name("/srv/hsync/bucket/photo.jpg", "cid123", "20250101")
        assert name.endswith("photo_conflicted_copy_20250101_cid123.jpg")

    results: list[BenchResult] = [
        measure("arbitrate_upload(first)", op_first, count=count),
        measure("arbitrate_upload(dominant)", op_dominant, count=count),
        measure("arbitrate_upload(superseded)", op_superseded, count=count),
        measure("arbitrate_upload(conflict_won)", op_conflict_won, count=count),
        measure("arbitrate_upload(conflict_lost)", op_conflict_lost, count=count),
        measure("conflicted_copy_name", op_conflicted_name, count=count),
    ]

    print(f"arbitration micro-benchmark (ops={count}, temp state dir)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
