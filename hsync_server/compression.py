# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Server.

This module provides file compression/decompression using Zstandard (zstd),
with intelligent decision-making on when compression is beneficial.
"""

from os.path import splitext

import zstandard as zstd
from loguru import logger as log

# Compression level (1-22, default 3 is a good balance of speed/ratio)
COMPRESSION_LEVEL = 3

# Content-Encoding header value for zstd compression
CONTENT_ENCODING_ZSTD = "zstd"

# =============================================================================
# Compression Thresholds
# =============================================================================
# Different thresholds based on transfer direction.
# Server primarily handles downloads (sending files to clients).
#
# Download (Server→Client): 10KB threshold
# - Below 10KB, compression overhead often exceeds transfer time savings
# - Above 10KB, compression benefits clearly outweigh overhead
# =============================================================================

# Download threshold: 10KB (server sends to client)
MIN_SIZE_FOR_DOWNLOAD = 10 * 1024  # 10 KB

# Legacy constant for backward compatibility
MIN_SIZE_FOR_COMPRESSION = MIN_SIZE_FOR_DOWNLOAD

# File extensions that are already compressed (compression won't help)
PRECOMPRESSED_EXTENSIONS = frozenset(
    {
        # Archives
        ".zip",
        ".gz",
        ".gzip",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
        ".zst",
        ".zstd",
        ".lz4",
        ".lzma",
        ".cab",
        ".dmg",
        ".pkg",
        ".msi",
        ".deb",
        ".rpm",
        ".apk",
        ".ipa",
        ".jar",
        ".war",
        ".ear",
        ".nupkg",
        # Images
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".heic",
        ".heif",
        ".jp2",
        ".j2k",
        ".jpf",
        ".jpm",
        ".jpx",
        ".tiff",
        ".tif",
        ".psd",
        ".ico",
        ".icns",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
        ".orf",
        ".rw2",
        # Audio
        ".mp3",
        ".aac",
        ".ogg",
        ".opus",
        ".flac",
        ".m4a",
        ".wma",
        ".alac",
        # Video
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".3gp",
        ".m2ts",
        ".mts",
        ".vob",
        ".ogv",
        # Documents (already compressed internally)
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".pages",
        ".numbers",
        ".keynote",
        ".xps",
        ".cbz",
        ".cbr",
        # Fonts
        ".woff",
        ".woff2",
        ".eot",
        ".ttc",
        # Other
        ".sqlite",
        ".db",
    }
)

# Create reusable compressor/decompressor for efficiency
_compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
_decompressor = zstd.ZstdDecompressor()


def should_compress(filepath: str, size: int) -> bool:
    """
    Determine if a file should be compressed before transfer.

    Args:
        filepath: Path to the file.
        size: Size of the file in bytes.

    Returns:
        True if file should be compressed, False otherwise.
    """
    # Too small to benefit from compression
    if size < MIN_SIZE_FOR_COMPRESSION:
        log.debug(f"Skip compression: {filepath} too small ({size} bytes)")
        return False

    # Check if file extension indicates pre-compressed format
    ext = splitext(filepath.lower())[1]
    if ext in PRECOMPRESSED_EXTENSIONS:
        log.debug(f"Skip compression: {filepath} already compressed ({ext})")
        return False

    return True


def compress_data(data: bytes) -> bytes:
    """
    Compress data using Zstandard.

    Args:
        data: Raw bytes to compress.

    Returns:
        Compressed bytes.
    """
    return _compressor.compress(data)


def decompress_data(data: bytes) -> bytes:
    """
    Decompress Zstandard-compressed data.

    Args:
        data: Compressed bytes.

    Returns:
        Decompressed bytes.
    """
    return _decompressor.decompress(data)


def compress_file(filepath: str) -> bytes:
    """
    Read and compress a file.

    Args:
        filepath: Path to file to compress.

    Returns:
        Compressed file content.
    """
    with open(filepath, "rb") as f:
        return compress_data(f.read())


async def async_compress_file(filepath: str, chunk_size: int = 64 * 1024) -> bytes:
    """
    Read and compress a file asynchronously using streaming compression.

    Reads the file in chunks using async I/O and compresses using zstd's
    streaming API, producing a single compressed frame. This avoids loading
    the entire uncompressed file into memory at once.

    The file size is passed to the stream writer so that the content size
    is embedded in the frame header, allowing the client to decompress
    without knowing the output size in advance.

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
