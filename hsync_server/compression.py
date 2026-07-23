# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Server.

Extends hsync_common.compression with server-specific async helpers.
"""

from collections.abc import AsyncIterator

import zstandard as zstd

# Re-export core compression from hsync-common
from hsync_common.compression import (  # noqa: F401
    COMPRESSION_LEVEL,
    CONTENT_ENCODING_ZSTD,
    MIN_SIZE_FOR_COMPRESSION,
    MIN_SIZE_FOR_DOWNLOAD,
    PRECOMPRESSED_EXTENSIONS,
    compress_data,
    compress_file,
    decompress_data,
    decompress_stream,
    should_compress,
)
from loguru import logger as log


class _StreamSink:
    """Minimal writable sink that buffers zstd output for incremental draining.

    ``ZstdCompressor.stream_writer`` pushes compressed bytes by calling
    ``write()``; this sink accumulates them so the surrounding async generator
    can pull (and yield) whatever is available after each input chunk.
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


async def async_compress_stream(filepath: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """
    Stream-compress a file, yielding zstd output chunks.

    Reads the file in chunks via async I/O and feeds them to zstd's streaming
    compressor, yielding compressed bytes as they are produced. The result is a
    single zstd frame with the decompressed size embedded in the header (so
    one-shot client/Android decompression keeps working), while neither the whole
    file nor the whole compressed output is ever held in memory. This bounds
    server memory usage and prevents a memory-bomb DoS on large downloads.

    Args:
        filepath: Path to file to compress.
        chunk_size: Size of read chunks in bytes (default: 64KB).

    Yields:
        Compressed chunks of a single zstd frame.
    """
    from pathlib import Path

    import aiofiles

    file_size = Path(filepath).stat().st_size
    sink = _StreamSink()
    compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
    with compressor.stream_writer(sink, size=file_size, closefd=False) as writer:  # type: ignore[arg-type]
        async with aiofiles.open(filepath, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                out = sink.drain()
                if out:
                    yield out
    # stream_writer.__exit__ flushes the final frame epilogue into the sink.
    out = sink.drain()
    if out:
        yield out


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
