"""
Tests for compression utilities.
"""

import pytest
from hsync_common.compression import (
    MIN_SIZE_FOR_COMPRESSION,
    compress_data,
    compress_file,
    decompress_data,
)

from hsync_server import compression


async def _compress_whole(filepath: str, chunk_size: int = 64 * 1024) -> bytes:
    """Join the streamed zstd frames into a single bytes object (test helper)."""
    parts = [chunk async for chunk in compression.async_compress_stream(filepath, chunk_size)]
    return b"".join(parts)


class TestCompression:
    """Tests for compression module functions."""

    def test_compress_data(self):
        """Test data compression round-trip."""
        # Use larger data to benefit from compression
        original_data = b"Hello, World! " * 100
        compressed = compress_data(original_data)
        assert compressed != original_data
        assert len(compressed) < len(original_data)
        # Verify round-trip: decompress must restore original data
        decompressed = decompress_data(compressed)
        assert decompressed == original_data

    def test_decompress_data(self):
        """Test data decompression."""
        original_data = b"Hello, World! This is test data for compression."
        compressed = compress_data(original_data)
        decompressed = decompress_data(compressed)
        assert decompressed == original_data

    def test_decompress_stream(self):
        """Streaming decompression yields the original content in chunks."""
        import io

        original_data = b"Test data for streaming decompression " * 1000
        compressed = compress_data(original_data)

        chunks = list(
            compression.decompress_stream(
                io.BytesIO(compressed), max_output_size=len(original_data)
            )
        )
        assert b"".join(chunks) == original_data

    def test_decompress_stream_cap_exceeded(self):
        """A payload expanding beyond max_output_size is rejected (anti-bomb)."""
        import io

        import zstandard as zstd

        original_data = b"B" * 100_000
        compressed = compress_data(original_data)

        with pytest.raises(zstd.ZstdError):
            list(compression.decompress_stream(io.BytesIO(compressed), max_output_size=100))

    def test_compress_file(self, tmp_path):
        """Test file compression round-trip."""
        test_file = tmp_path / "test.txt"
        # Use larger content to benefit from compression
        test_content = b"Test content for compression " * 50
        test_file.write_bytes(test_content)

        compressed = compress_file(str(test_file))
        assert compressed != test_content
        assert len(compressed) < len(test_content)
        # Verify round-trip: decompress must restore original content
        decompressed = decompress_data(compressed)
        assert decompressed == test_content

    def test_async_compress_file(self, tmp_path):
        """Test async streaming file compression produces single decompressible frame."""
        import asyncio

        test_file = tmp_path / "test_async.txt"
        # Content larger than chunk_size (64KB) to test multi-chunk reading
        test_content = b"Async compress test data " * 5000  # ~120KB
        test_file.write_bytes(test_content)

        async def run_test():
            return await _compress_whole(str(test_file))

        compressed = asyncio.run(run_test())
        assert compressed != test_content
        assert len(compressed) < len(test_content)

        # Verify it decompresses correctly (single frame, client-compatible)
        decompressed = decompress_data(compressed)
        assert decompressed == test_content

    def test_async_compress_file_matches_sync(self, tmp_path):
        """Test async compression produces same decompressed result as sync."""
        import asyncio

        test_file = tmp_path / "test_match.txt"
        test_content = b"Match test data " * 8000  # ~128KB
        test_file.write_bytes(test_content)

        sync_compressed = compress_file(str(test_file))

        async def run_test():
            return await _compress_whole(str(test_file))

        async_compressed = asyncio.run(run_test())

        # Both should decompress to the same content
        assert decompress_data(sync_compressed) == test_content
        assert decompress_data(async_compressed) == test_content

    def test_async_compress_file_small_file(self, tmp_path):
        """Test async compression works for files smaller than chunk size."""
        import asyncio

        test_file = tmp_path / "small.txt"
        test_content = b"Small file content"
        test_file.write_bytes(test_content)

        async def run_test():
            return await _compress_whole(str(test_file))

        compressed = asyncio.run(run_test())
        decompressed = decompress_data(compressed)
        assert decompressed == test_content

    def test_async_compress_stream_yields_decompressible_frame(self, tmp_path):
        """Streaming compression yields a single one-shot-decompressible frame."""
        import asyncio

        test_file = tmp_path / "stream.txt"
        # Larger than chunk_size to force multiple yielded chunks.
        test_content = b"Streaming compress payload " * 8000  # ~216KB
        test_file.write_bytes(test_content)

        async def run_test():
            return [
                chunk
                async for chunk in compression.async_compress_stream(
                    str(test_file), chunk_size=16 * 1024
                )
            ]

        chunks = asyncio.run(run_test())
        # Memory stays bounded: the output is produced incrementally.
        assert len(chunks) > 1
        compressed = b"".join(chunks)
        assert len(compressed) < len(test_content)
        # One-shot decompression must work (size embedded in the frame header).
        assert decompress_data(compressed) == test_content

    def test_async_compress_stream_matches_whole_file(self, tmp_path):
        """Streaming and whole-file compression produce identical bytes."""
        import asyncio

        test_file = tmp_path / "match_stream.txt"
        test_content = b"Identical frame data " * 9000  # ~189KB
        test_file.write_bytes(test_content)

        async def run_test():
            whole = await _compress_whole(str(test_file))
            streamed = b"".join(
                [chunk async for chunk in compression.async_compress_stream(str(test_file))]
            )
            return whole, streamed

        whole, streamed = asyncio.run(run_test())
        assert whole == streamed


class TestShouldCompress:
    """Tests for should_compress function."""

    def test_small_file_returns_false(self):
        """Files below threshold should not be compressed."""
        assert compression.should_compress("test.txt", 1024) is False

    def test_large_file_returns_true(self):
        """Files above threshold should be compressed."""
        assert compression.should_compress("test.txt", 20 * 1024) is True

    def test_precompressed_extension_jpg(self):
        """JPG files should not be compressed."""
        assert compression.should_compress("image.jpg", 20 * 1024) is False

    def test_precompressed_extension_png(self):
        """PNG files should not be compressed."""
        assert compression.should_compress("image.png", 20 * 1024) is False

    def test_precompressed_extension_pdf(self):
        """PDF files should not be compressed."""
        assert compression.should_compress("document.pdf", 20 * 1024) is False

    def test_precompressed_extension_zip(self):
        """ZIP files should not be compressed."""
        assert compression.should_compress("archive.zip", 20 * 1024) is False

    def test_precompressed_extension_mp3(self):
        """MP3 files should not be compressed."""
        assert compression.should_compress("audio.mp3", 20 * 1024) is False

    def test_precompressed_extension_mp4(self):
        """MP4 files should not be compressed."""
        assert compression.should_compress("video.mp4", 20 * 1024) is False

    def test_compressible_extension_txt(self):
        """TXT files should be compressed."""
        assert compression.should_compress("document.txt", 20 * 1024) is True

    def test_compressible_extension_json(self):
        """JSON files should be compressed."""
        assert compression.should_compress("data.json", 20 * 1024) is True

    def test_compressible_extension_py(self):
        """PY files should be compressed."""
        assert compression.should_compress("script.py", 20 * 1024) is True


class TestConstants:
    """Tests for compression constants."""

    def test_compression_level_is_valid(self):
        """Compression level should be in valid range."""
        assert 1 <= compression.COMPRESSION_LEVEL <= 22

    def test_content_encoding_zstd(self):
        """Content encoding should be 'zstd'."""
        assert compression.CONTENT_ENCODING_ZSTD == "zstd"

    def test_min_size_for_compression(self):
        """Minimum compression (download) size should be 10KB."""
        assert MIN_SIZE_FOR_COMPRESSION == 10 * 1024


class TestConcurrentStreamedCompression:
    """Concurrent compressed downloads must never share a zstd compressor.

    ``python-zstandard`` instances are not safe for concurrent or interleaved
    operations: one shared ``ZstdCompressor`` corrupts frames (``ZstdError:
    Src size is incorrect``) and can segfault the process. ``async_compress_stream``
    runs its ``stream_writer.write`` calls on the event-loop thread, so a
    thread-local pool cannot isolate concurrent streams either — each call must
    own its compressor.
    """

    STREAMS = 6

    def test_concurrent_streams_produce_valid_frames(self, tmp_path):
        import asyncio

        contents: dict[str, bytes] = {}
        for i in range(self.STREAMS):
            data = bytes([(i * 40) % 256]) * 300_000  # ~300 KB, distinct per stream
            path = tmp_path / f"stream_{i}.bin"
            path.write_bytes(data)
            contents[str(path)] = data

        async def run() -> list[bytes]:
            # Small chunks maximize the interleaving window between streams.
            return await asyncio.gather(
                *(_compress_whole(path, chunk_size=4096) for path in contents)
            )

        frames = asyncio.run(run())
        for (path, data), frame in zip(contents.items(), frames, strict=True):
            assert decompress_data(frame, max_output_size=len(data) + 1) == data, path

    def test_concurrent_stream_frames_match_serial_frames(self, tmp_path):
        """A stream compressed under concurrency is byte-identical to the
        serial one: no cross-stream frames, no duplicated or missing bytes."""
        import asyncio

        contents: dict[str, bytes] = {}
        for i in range(self.STREAMS):
            data = b"identical frame check " * 20_000  # same shape, same size
            path = tmp_path / f"match_{i}.bin"
            path.write_bytes(data)
            contents[str(path)] = data

        async def run() -> list[bytes]:
            return await asyncio.gather(*(_compress_whole(path) for path in contents))

        concurrent = asyncio.run(run())
        serial = [asyncio.run(_compress_whole(path)) for path in contents]
        assert concurrent == serial
