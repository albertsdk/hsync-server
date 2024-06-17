"""
Tests for compression utilities.
"""

import pytest

from hsync_server import compression


class TestCompression:
    """Tests for compression module functions."""

    def test_compress_data(self):
        """Test data compression."""
        # Use larger data to benefit from compression
        original_data = b"Hello, World! " * 100
        compressed = compression.compress_data(original_data)
        assert compressed != original_data
        assert len(compressed) < len(original_data)

    def test_decompress_data(self):
        """Test data decompression."""
        original_data = b"Hello, World! This is test data for compression."
        compressed = compression.compress_data(original_data)
        decompressed = compression.decompress_data(compressed)
        assert decompressed == original_data

    def test_decompress_stream(self):
        """Test async stream decompression."""
        import asyncio

        original_data = b"Test data for async decompression"
        compressed = compression.compress_data(original_data)

        async def run_test():
            return await compression.decompress_stream(compressed)

        result = asyncio.run(run_test())
        assert result == original_data

    def test_compress_file(self, tmp_path):
        """Test file compression."""
        test_file = tmp_path / "test.txt"
        # Use larger content to benefit from compression
        test_content = b"Test content for compression " * 50
        test_file.write_bytes(test_content)

        compressed = compression.compress_file(str(test_file))
        assert compressed != test_content
        assert len(compressed) < len(test_content)

    def test_async_compress_file(self, tmp_path):
        """Test async streaming file compression produces single decompressible frame."""
        import asyncio

        test_file = tmp_path / "test_async.txt"
        # Content larger than chunk_size (64KB) to test multi-chunk reading
        test_content = b"Async compress test data " * 5000  # ~120KB
        test_file.write_bytes(test_content)

        async def run_test():
            return await compression.async_compress_file(str(test_file))

        compressed = asyncio.run(run_test())
        assert compressed != test_content
        assert len(compressed) < len(test_content)

        # Verify it decompresses correctly (single frame, client-compatible)
        decompressed = compression.decompress_data(compressed)
        assert decompressed == test_content

    def test_async_compress_file_matches_sync(self, tmp_path):
        """Test async compression produces same decompressed result as sync."""
        import asyncio

        test_file = tmp_path / "test_match.txt"
        test_content = b"Match test data " * 8000  # ~128KB
        test_file.write_bytes(test_content)

        sync_compressed = compression.compress_file(str(test_file))

        async def run_test():
            return await compression.async_compress_file(str(test_file))

        async_compressed = asyncio.run(run_test())

        # Both should decompress to the same content
        assert compression.decompress_data(sync_compressed) == test_content
        assert compression.decompress_data(async_compressed) == test_content

    def test_async_compress_file_small_file(self, tmp_path):
        """Test async compression works for files smaller than chunk size."""
        import asyncio

        test_file = tmp_path / "small.txt"
        test_content = b"Small file content"
        test_file.write_bytes(test_content)

        async def run_test():
            return await compression.async_compress_file(str(test_file))

        compressed = asyncio.run(run_test())
        decompressed = compression.decompress_data(compressed)
        assert decompressed == test_content


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

    def test_min_size_for_download(self):
        """Minimum download size should be 10KB."""
        assert compression.MIN_SIZE_FOR_DOWNLOAD == 10 * 1024

    def test_min_size_for_compression_alias(self):
        """Backward compatibility alias should match."""
        assert compression.MIN_SIZE_FOR_COMPRESSION == compression.MIN_SIZE_FOR_DOWNLOAD
