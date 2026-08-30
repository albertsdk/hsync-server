# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server rate-limiting micro-benchmark (pure: IP resolution + token bucket).

Every REST request resolves the client IP through the trusted-proxy gate and,
for WebSocket connections, every event consumes a ``TokenBucket`` token. This
benchmark measures the pure decision layers, not slowapi/network I/O:

* ``get_client_ip`` — X-Forwarded-For honoured only from a trusted proxy, else
  the direct peer address (``rate_limiter.py:74``);
* ``_parse_ws_rate`` — ``"<n>/<period>"`` → (tokens/s, burst) (``:170``);
* ``TokenBucket.acquire`` — the refill+consume logic on the always-hit and
  always-drained branches (``:220``).

Measured ops assert the exact returned value/decision so a silently wrong IP or
a broken token budget cannot be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_rate_limiter.py [--count N] [--json-out FILE]
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

import hsync_server.rate_limiter as rate_limiter  # noqa: E402


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host


class _Headers(dict):
    pass


class _Request:
    """Minimal Starlette-Request stand-in exposing only what ``get_client_ip`` reads."""

    def __init__(self, host: str | None, *, forwarded: str | None = None) -> None:
        self.client = _Client(host) if host else None
        self.headers = _Headers({"X-Forwarded-For": forwarded}) if forwarded else _Headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    # The trusted-proxy set is a module global; pin it for a deterministic gate.
    rate_limiter.TRUSTED_PROXIES = frozenset({"10.0.0.1"})
    trusted = _Request("10.0.0.1", forwarded="203.0.113.7, 10.0.0.1")
    direct = _Request("10.0.0.2", forwarded="203.0.113.7")
    no_client = _Request(None)

    assert rate_limiter.get_client_ip(trusted) == "203.0.113.7"
    assert rate_limiter.get_client_ip(direct) == "10.0.0.2"
    assert rate_limiter.get_client_ip(no_client) == "127.0.0.1"
    assert rate_limiter._parse_ws_rate("10/second") == (10.0, 20)
    assert rate_limiter._parse_ws_rate("100/minute") == (100.0 * (1.0 / 60), 3)
    assert rate_limiter._parse_ws_rate("nonsense") == (10.0, 20)

    # Always-hit bucket: huge burst so 2000 consumes never drain it.
    full = rate_limiter.TokenBucket(rate=1e9, burst=1e9)
    # Always-empty bucket: no refill and zero capacity.
    empty = rate_limiter.TokenBucket(rate=0.0, burst=0)
    assert full.acquire() is True
    assert empty.acquire() is False

    def op_ip_trusted() -> None:
        assert rate_limiter.get_client_ip(trusted) == "203.0.113.7"

    def op_ip_direct() -> None:
        assert rate_limiter.get_client_ip(direct) == "10.0.0.2"

    def op_parse_rate() -> None:
        assert rate_limiter._parse_ws_rate("10/second") == (10.0, 20)

    def op_acquire_hit() -> None:
        assert full.acquire() is True

    def op_acquire_drained() -> None:
        assert empty.acquire() is False

    results: list[BenchResult] = [
        measure("get_client_ip(trusted+xff)", op_ip_trusted, count=count),
        measure("get_client_ip(direct)", op_ip_direct, count=count),
        measure("parse_ws_rate(10/second)", op_parse_rate, count=count),
        measure("token_bucket.acquire(hit)", op_acquire_hit, count=count),
        measure("token_bucket.acquire(drained)", op_acquire_drained, count=count),
    ]

    print(f"rate-limiter micro-benchmark (ops={count}, pure CPU, no I/O)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
