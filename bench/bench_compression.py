# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — server compression-path micro-benchmark (temp file, no persisted state).

Two transfer paths compress with zstd: ``async_compress_stream`` streams one
frame per compressed download, and the delta endpoint compresses its literal
payload one-shot. A ``ZstdCompressor`` instance is never shared between
concurrent or interleaved operations (python-zstandard 0.25 corrupts frames
and can segfault), so the streaming helper builds one compressor per call
and the one-shot path goes through hsync-common's thread-local
``compress_data``. This script prices that isolation:

* ``new_compressor(level=3)`` — the per-call construction the stream path pays
  (measured median ~1.1 us, i.e. negligible against a >=10 KB stream);
* ``compress_data(64KiB)`` — the delta-literal path (thread-local SSOT helper);
* ``async_compress_stream(256KiB|1MiB)`` — the full compressed-download helper:
  per-call compressor + worker-thread chunked read + single-frame flush.

Persist-heavy/streaming scenarios run at a lower effective sample count. Every
measured op asserts its branch (compressor usable, payload round-trips, the
streamed frame decodes to the exact source bytes), so a disabled or corrupted
path can never be reported as fast.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_compression.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import zstandard as zstd  # noqa: E402
from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    measure_async,
    print_table,
)
from hsync_common.compression import (  # noqa: E402
    COMPRESSION_LEVEL,
    compress_data,
    decompress_data,
)

from hsync_server.compression import async_compress_stream  # noqa: E402


def _payload(size: int, seed: int) -> bytes:
    """Deterministic compressible payload of *size* bytes."""
    block = bytes(((seed + i) % 251) + 1 for i in range(64))
    return (block * (size // len(block) + 1))[:size]


async def _stream_whole(path: str) -> bytes:
    """Join one compressed stream into the single zstd frame it produces."""
    return b"".join([chunk async for chunk in async_compress_stream(path)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count
    stream_count = max(10, count // 100)
    stream_count_large = max(5, count // 200)

    payload_64k = _payload(64 * 1024, seed=1)

    with tempfile.TemporaryDirectory(prefix="hsync_compression_") as tmp:
        file_256k = Path(tmp) / "payload_256k.bin"
        file_1m = Path(tmp) / "payload_1m.bin"
        content_256k = _payload(256 * 1024, seed=2)
        content_1m = _payload(1024 * 1024, seed=3)
        file_256k.write_bytes(content_256k)
        file_1m.write_bytes(content_1m)

        def op_new_compressor() -> None:
            compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
            assert compressor.memory_size() > 0

        def op_compress_data() -> None:
            frame = compress_data(payload_64k, level=COMPRESSION_LEVEL)
            assert decompress_data(frame, max_output_size=len(payload_64k) + 1) == payload_64k

        async def op_stream_256k() -> None:
            frame = await _stream_whole(str(file_256k))
            assert decompress_data(frame, max_output_size=len(content_256k) + 1) == content_256k

        async def op_stream_1m() -> None:
            frame = await _stream_whole(str(file_1m))
            assert decompress_data(frame, max_output_size=len(content_1m) + 1) == content_1m

        results: list[BenchResult] = [
            measure("new_compressor(level=3)", op_new_compressor, count=count),
            measure("compress_data(64KiB)", op_compress_data, count=count),
        ]

        async def _run_streams() -> None:
            results.append(
                await measure_async(
                    "async_compress_stream(256KiB)", op_stream_256k, count=stream_count
                )
            )
            results.append(
                await measure_async(
                    "async_compress_stream(1MiB)", op_stream_1m, count=stream_count_large
                )
            )

        asyncio.run(_run_streams())

        print(
            f"compression micro-benchmark (ops={count}, level={COMPRESSION_LEVEL}, "
            f"stream ops={stream_count}/{stream_count_large})"
        )
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
