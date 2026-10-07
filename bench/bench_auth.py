# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — server auth micro-benchmark (pure: token parsing + KDF cache key).

Every REST/WS request resolves the Bearer account token through the same two
layers measured here: the wire token is parsed (``hs_<id 21>_<secret 64>``) and
the KDF result is cached under ``SHA-256(token)``. The scrypt verification
itself is deliberately not measured — it is the rare cache-miss cost and, by
design, is CPU-fixed (the anti-enumeration dummy burn) — so this benchmark
covers the per-request layers that run on every hit instead.

Measured ops assert the exact returned value so a silently wrong parse (accept
or reject) can never be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_auth.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Cheap scrypt for the one-time import dummy-hash (mirrors the test suite's
# ``fast_scrypt`` fixture): the benchmark measures parsing, not the KDF.
os.environ.setdefault("HSYNC_SCRYPT_N", "16")

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cid,
    make_hex,
    maybe_write_json,
    measure,
    print_table,
)

from hsync_server.auth import _cache_key  # noqa: E402
from hsync_server.auth_store import TOKEN_PREFIX, parse_token  # noqa: E402


def _token(n: int, *, secret: str | None = None, account_id: str | None = None) -> str:
    acc = account_id if account_id is not None else make_cid(n)
    sec = secret if secret is not None else make_hex(n)
    return f"{TOKEN_PREFIX}{acc}_{sec}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    valid = _token(1)
    expected = (make_cid(1), make_hex(1))
    bad_prefix = "xx_" + valid[len(TOKEN_PREFIX) :]
    bad_id = _token(2, account_id="!" * 21)
    bad_secret = _token(3, secret="z" * 64)
    short_secret = _token(4, secret="a" * 63)
    cache_key_expected = hashlib.sha256(valid.encode("utf-8")).hexdigest()

    assert parse_token(valid) == expected
    assert parse_token(bad_prefix) is None
    assert parse_token(bad_id) is None
    assert parse_token(bad_secret) is None
    assert parse_token(short_secret) is None
    assert _cache_key(valid) == cache_key_expected

    def op_parse_valid() -> None:
        assert parse_token(valid) == expected

    def op_parse_bad_prefix() -> None:
        assert parse_token(bad_prefix) is None

    def op_parse_bad_id() -> None:
        assert parse_token(bad_id) is None

    def op_parse_bad_secret() -> None:
        assert parse_token(bad_secret) is None

    def op_cache_key() -> None:
        assert _cache_key(valid) == cache_key_expected

    results: list[BenchResult] = [
        measure("parse_token(valid)", op_parse_valid, count=count),
        measure("parse_token(bad_prefix)", op_parse_bad_prefix, count=count),
        measure("parse_token(bad_id)", op_parse_bad_id, count=count),
        measure("parse_token(bad_secret)", op_parse_bad_secret, count=count),
        measure("auth_cache_key(sha256)", op_cache_key, count=count),
    ]

    print(f"auth micro-benchmark (ops={count}, pure CPU, no I/O)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
