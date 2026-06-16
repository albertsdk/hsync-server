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


async def async_compress_file(filepath: str, chunk_size: int = 64 * 1024) -> bytes:
    """
    Read and compress a file asynchronously into a single bytes object.

    Backward-compatible wrapper around :func:`async_compress_stream` for callers
    that need the whole compressed frame at once. Prefer the streaming variant
    for large downloads to keep memory bounded.

    Args:
        filepath: Path to file to compress.
        chunk_size: Size of read chunks in bytes (default: 64KB).

    Returns:
        Compressed file content as a single zstd frame.
    """
    parts = [chunk async for chunk in async_compress_stream(filepath, chunk_size)]
    return b"".join(parts)


async def decompress_stream(data: bytes) -> bytes:
    """
    Decompress data asynchronously (for FastAPI handlers).

    Args:
        data: Compressed bytes.

    Returns:
        Decompressed bytes.
    """
    return decompress_data(data)


log.debug(f"Module {__name__} loaded")
