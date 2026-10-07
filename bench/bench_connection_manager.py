# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — server connection-slot micro-benchmark (pure accounting).

The DoS-protection gate every WebSocket join passes through:
:meth:`BucketConnectionManager.try_acquire_slot` enforces a global and a per-IP
connection cap, and :meth:`release_slot` releases them on disconnect
(`api/connection_manager.py:139,:151`). The broadcast/authz re-check path is
socket- and auth-store-bound and intentionally left to integration tests.

The config getters are pinned (``get_ws_max_connections``/``..._per_ip``) so the
three acquisition branches — under budget, global cap reached, per-IP cap
reached — and the release branch run deterministically.

Measured ops assert the True/False verdict so a broken cap cannot be reported as
fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_connection_manager.py [--count N] [--json-out FILE]
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
    maybe_write_json,
    measure,
    print_table,
)

import hsync_server.api.connection_manager as connection_manager  # noqa: E402
from hsync_server.api.connection_manager import BucketConnectionManager  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    # The cap branches log one warning per sample; silence the sink.
    from loguru import logger as log

    log.remove()

    # Pin the caps: huge for the "under budget" branch, then small for the caps.
    connection_manager.get_ws_max_connections = lambda: 10**9  # type: ignore[assignment]
    connection_manager.get_ws_max_connections_per_ip = lambda: 10**9  # type: ignore[assignment]
    under = BucketConnectionManager()
    assert under.try_acquire_slot("10.0.0.1") is True

    def op_acquire_under() -> None:
        assert under.try_acquire_slot("10.0.0.1") is True

    # Measure the under-budget branch while the huge caps are in effect (the
    # measurement loop runs after this function body, so it must be captured now).
    results: list[BenchResult] = [
        measure("try_acquire_slot(under)", op_acquire_under, count=count),
    ]

    connection_manager.get_ws_max_connections = lambda: 5  # type: ignore[assignment]
    connection_manager.get_ws_max_connections_per_ip = lambda: 2  # type: ignore[assignment]
    full = BucketConnectionManager()
    full._total_slots = 5
    ip_full = BucketConnectionManager()
    ip_full._slots_per_ip = {"10.0.0.1": 2}
    assert full.try_acquire_slot("10.0.0.2") is False  # global cap
    assert ip_full.try_acquire_slot("10.0.0.1") is False  # per-IP cap

    release = BucketConnectionManager()

    def op_acquire_global_cap() -> None:
        assert full.try_acquire_slot("10.0.0.2") is False

    def op_acquire_ip_cap() -> None:
        assert ip_full.try_acquire_slot("10.0.0.1") is False

    def op_release() -> None:
        release._total_slots = 1
        release._slots_per_ip = {"10.0.0.1": 1}
        release.release_slot("10.0.0.1")
        assert release._total_slots == 0 and "10.0.0.1" not in release._slots_per_ip

    results += [
        measure("try_acquire_slot(global_cap)", op_acquire_global_cap, count=count),
        measure("try_acquire_slot(per_ip_cap)", op_acquire_ip_cap, count=count),
        measure("release_slot", op_release, count=count),
    ]

    print(f"connection-slot micro-benchmark (ops={count}, pure accounting)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
