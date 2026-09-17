# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the download-side compression contract.

Drives the real server over HTTP only. The upload suite covers compressed
*uploads*; this file covers the symmetric *download* path: files above the
10 KB download threshold are streamed zstd-compressed with ``Content-Encoding:
zstd`` and must round-trip to the original bytes, while every download below the
threshold is served identity — the HTTP gzip layer must never re-encode a
transfer payload, which clients reject outright.

Requires HSYNC_BB_SERVER=1 to run.
"""

import os

import httpx
import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]


class TestDownloadCompressed:
    def test_large_compressible_file_downloads_compressed(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an uploaded file large enough (and compressible enough) to
        # cross the 10 KB download-compression threshold
        original = b"compressible download payload " * 2000  # ~56 KB
        bb_upload(bb_http, bb_bucket, "big.txt", original)

        # When downloading it (no Range header)
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "big.txt"}
        )

        # Then the server advertises zstd compression and the body (decoded by
        # the HTTP client) round-trips to the exact original bytes. The header
        # is only set by the compressed branch, so its presence proves the
        # threshold decision fired.
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        assert "X-Checksum-SHA256" in r.headers
        assert r.content == original

    def test_compressed_download_of_non_latin1_name(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an uploaded file that crosses the compression threshold and
        # whose name contains a character outside the latin-1 header repertoire
        # ('č' U+010D) — the compressed branch builds Content-Disposition itself
        original = b"compressible payload " * 4000  # ~76 KB
        filename = "dati-\u010d-large.bin"
        bb_upload(bb_http, bb_bucket, filename, original)

        # When downloading it
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": filename}
        )

        # Then the non-ASCII name is advertised with the RFC 5987 form instead
        # of raising UnicodeEncodeError while serializing the headers (a 500),
        # and the payload still round-trips exactly.
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        assert r.headers.get("Content-Disposition") == (
            "attachment; filename*=utf-8''dati-%C4%8D-large.bin"
        )
        assert r.content == original


class TestTransferPayloadNotReEncoded:
    """The transfer endpoints must never be re-encoded by the HTTP gzip layer.

    Below the zstd threshold the server serves the file byte-identical with an
    ordinary ``Content-Length``: the hsync client accepts only ``identity`` or
    ``Content-Encoding: zstd`` on a download and aborts the transfer on any
    other coding, and chunked gzip would also invalidate the Range/resume byte
    offsets. ``hsync_server.middleware.TransferAwareGZipMiddleware`` keeps the
    generic compressor off these paths, while API metadata responses stay
    gzipped.
    """

    def test_small_download_is_not_gzipped(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given an uploaded file below the 10 KB download-compression threshold
        original = b"below the compression threshold\n"
        bb_upload(bb_http, bb_bucket, "small.txt", original)

        # When a client that advertises gzip at the HTTP layer downloads it
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": "small.txt"},
            headers={"Accept-Encoding": "gzip"},
        )

        # Then the payload carries no HTTP content coding and keeps the exact
        # byte length the client checks before hashing
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") is None
        assert r.headers.get("Content-Length") == str(len(original))
        assert r.content == original

    def test_large_api_listing_is_still_gzipped(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given enough files for the listing JSON to cross the 1000-byte
        # gzip threshold
        for i in range(12):
            bb_upload(bb_http, bb_bucket, f"pad-{i:03d}.txt", b"x" * 10)

        # When listing them with a client that advertises gzip
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files",
            headers={"Accept-Encoding": "gzip"},
        )

        # Then the metadata response is still compressed (only transfer
        # payloads are excluded) and decodes to the full listing
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "gzip"
        assert len(r.json()) == 12
