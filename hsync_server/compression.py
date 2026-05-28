# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Server.

Extends hsync_common.compression with server-specific async helpers.
"""

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


async def async_compress_file(filepath: str, chunk_size: int = 64 * 1024) -> bytes:
    """
    Read and compress a file asynchronously using streaming compression.

    Reads the file in chunks using async I/O and compresses using zstd's
    streaming API, producing a single compressed frame.

    Args:
        filepath: Path to file to compress.
        chunk_size: Size of read chunks in bytes (default: 64KB).

    Returns:
        Compressed file content as a single zstd frame.
    """
    import io
    from os.path import getsize

    import aiofiles

    file_size = getsize(filepath)
    output = io.BytesIO()
    compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
    with compressor.stream_writer(output, size=file_size, closefd=False) as writer:
        async with aiofiles.open(filepath, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
    return output.getvalue()


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
