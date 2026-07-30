# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the download-side compression use case.

Drives the real server over HTTP only. The upload suite covers compressed
*uploads*; this file covers the symmetric *download* path: files above the
10 KB download threshold are streamed zstd-compressed with ``Content-Encoding:
zstd`` and must round-trip to the original bytes.

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
