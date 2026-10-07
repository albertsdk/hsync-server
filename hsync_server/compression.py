# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Compression utilities for HSYNC Server.

Extends hsync_common.compression with server-specific async helpers.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import aiofiles
import zstandard as zstd
from hsync_common.compression import (  # noqa: F401  # CONTENT_ENCODING_ZSTD, should_compress re-exported
    COMPRESSION_LEVEL,
    CONTENT_ENCODING_ZSTD,
    decompress_stream,
    should_compress,
)
from hsync_common.runtime import get_cpu_quota_cores
from loguru import logger as log

# A zstd compressor instance is never shared: concurrent or interleaved
# operations on one instance (``stream_writer`` included) corrupt frames and
# can segfault the process (python-zstandard 0.25.0). Each
# ``async_compress_stream`` call owns its compressor for the whole transfer.
# Its ``write``/``close`` calls are always awaited one at a time, so although
# the executor may run them on different threads they are never concurrent:
# python-zstandard keeps no thread-local state, only per-instance state.
#
# Compression runs on a dedicated worker pool; the file read stays on the loop
# via aiofiles. One-shot callers on worker threads use hsync-common's
# thread-local ``compress_data`` (api/files/delta/signatures.py).

# Reads are coalesced into one offloaded ``stream_writer.write`` per batch. The
# hop costs ~27 us, so batching keeps the write overhead at ~1 hop/MiB instead
# of one per 64 KiB read, while memory stays bounded to a single batch per
# transfer. 1 MiB is kept deliberately: a larger batch cuts a few more hops but
# the consumer receives nothing until the whole batch is compressed, and a sweep
# measured first-byte latency growing steeply with the batch size.
_COMPRESS_BATCH_BYTES = 1024 * 1024


# Compression is CPU-bound and a worker is only held for the duration of one
# batch (never while parked), so the pool is sized to the CPU budget rather than
# to the number of transfers. ``os.cpu_count()`` reports host cores and ignores
# a container's CFS quota (``--cpus``/compose/Kubernetes), so an effective quota
# is used when present to avoid over-provisioning CPU-bound workers.
def _resolve_compress_workers() -> int:
    cores = os.cpu_count() or 1
    quota = get_cpu_quota_cores()
    if quota is not None:
        cores = max(1, min(cores, math.ceil(quota)))
    return cores


_COMPRESS_MAX_WORKERS = _resolve_compress_workers()

_compress_executor: ThreadPoolExecutor | None = None
_compress_executor_lock = threading.Lock()


def _get_compress_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded executor used for zstd compression."""
    global _compress_executor
    executor = _compress_executor
    if executor is None:
        with _compress_executor_lock:
            executor = _compress_executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=_COMPRESS_MAX_WORKERS,
                    thread_name_prefix="hsync-zstd",
                )
                _compress_executor = executor
    return executor


class _StreamSink:
    """Minimal writable sink that buffers zstd output for incremental draining.

    ``ZstdCompressor.stream_writer`` pushes compressed bytes by calling
    ``write()``; this sink accumulates them so the async generator can drain
    (and yield) whatever is available after each input batch.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def drain(self) -> bytes:
        data = bytes(self._buf)
        self._buf.clear()
        return data


def _open_writer(compressor: zstd.ZstdCompressor, sink: _StreamSink, size: int):
    """Build the streaming writer on a worker thread (off the event loop)."""
    return compressor.stream_writer(sink, size=size, closefd=False)  # type: ignore[arg-type]


def _write_batch(writer, data: bytes) -> None:
    writer.write(data)


def _close_writer(writer) -> None:
    writer.close()


async def async_compress_stream(filepath: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """
    Stream-compress a file, yielding zstd output chunks.

    The file is read on the event loop (aiofiles) and the reads are coalesced
    into batches that are compressed on a dedicated worker pool, so a multi-GB
    download never runs zstd on the loop. Because a worker is only held while a
    batch is compressed (not while the consumer is blocked on backpressure),
    slow readers cannot starve other downloads. The result is a single zstd
    frame with the decompressed size embedded in the header (so one-shot
    client/Android decompression keeps working) and memory bounded to one batch.

    Args:
        filepath: Path to file to compress.
        chunk_size: Size of read chunks in bytes (default: 64KB).

    Yields:
        Compressed chunks of a single zstd frame.
    """
    loop = asyncio.get_running_loop()
    executor = _get_compress_executor()
    file_size = os.stat(filepath).st_size
    sink = _StreamSink()
    compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
    # Construction touches zstd: keep it off the loop like the writes.
    writer = await loop.run_in_executor(executor, _open_writer, compressor, sink, file_size)
    batch = bytearray()
    try:
        async with aiofiles.open(filepath, "rb") as src:
            while True:
                chunk = await src.read(chunk_size)
                if not chunk:
                    break
                batch.extend(chunk)
                if len(batch) >= _COMPRESS_BATCH_BYTES:
                    await loop.run_in_executor(executor, _write_batch, writer, bytes(batch))
                    batch.clear()
                    out = sink.drain()
                    if out:
                        yield out
        if batch:
            await loop.run_in_executor(executor, _write_batch, writer, bytes(batch))
            batch.clear()
        # Finalize the single frame off the loop: the epilogue compresses the
        # last block, so this is not free.
        await loop.run_in_executor(executor, _close_writer, writer)
        out = sink.drain()
        if out:
            yield out
    finally:
        # No ``await`` here: an async generator closed via ``aclose()`` (client
        # disconnect) must unwind without awaiting. ``writer`` holds only native
        # memory, freed by GC; an aborted transfer yields a truncated frame,
        # which the client discards (same as the previous implementation).
        batch.clear()


def decompress_file_to_file(src_path: str, dst_path: str, max_output_size: int) -> int:
    """Expand a zstd-compressed file into another file, streaming.

    Blocking by design (sync file I/O + CPU-bound decompression): call it via
    ``asyncio.to_thread`` from async handlers so the event loop stays free.

    Args:
        src_path: Path of the compressed input file.
        dst_path: Path of the output file to create/overwrite.
        max_output_size: Hard cap on the expanded bytes (anti
            decompression-bomb); a :class:`zstandard.ZstdError` is raised
            before the cap is exceeded.

    Returns:
        The number of decompressed bytes written (fsync'd before returning).
    """
    from os import fsync

    total = 0
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        for chunk in decompress_stream(src, max_output_size=max_output_size):
            dst.write(chunk)
            total += len(chunk)
        dst.flush()
        fsync(dst.fileno())
    return total


log.debug(f"Module {__name__} loaded")
