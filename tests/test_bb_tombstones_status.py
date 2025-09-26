# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for tombstone listing and resumable upload-status.

Drives the real server over HTTP only. Verifies observable behaviour of the
deletion-marker and resume-status endpoints without touching internals.
"""

import hashlib
import os

import httpx
import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]

_CID = "bbclient0001bbcltBB10"


class TestTombstones:
    def test_empty_bucket_has_no_tombstones(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a freshly created bucket
        # When listing tombstones
        r = bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones")

        # Then no deletion markers are present
        assert r.status_code == 200
        assert r.json() == {}

    def test_tombstones_on_missing_bucket_returns_404(self, bb_http: httpx.Client):
        # Given a bucket that does not exist
        # When listing its tombstones
        r = bb_http.get("/api/v1/buckets/bb-no-such-bucket/tombstones")

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_tombstone_entry_has_timestamp_and_sha(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a deleted file with known content
        content = b"to be deleted"
        bb_upload(bb_http, bb_bucket, "t.bin", content)
        del_r = bb_http.request(
            "DELETE",
            f"/api/v1/buckets/{bb_bucket}/files",
            data={"cid": _CID, "filename": "t.bin"},
        )
        assert del_r.status_code == 200

        # When listing tombstones
        payload = bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones").json()

        # Then the single entry carries a timestamp and the correct content hash
        assert len(payload) == 1
        entry = next(iter(payload.values()))
        assert isinstance(entry["ts"], int)
        expected_sha = hashlib.sha256(content).hexdigest()
        assert entry["sha256"] == expected_sha


class TestUploadStatus:
    def test_new_file_reports_new_status(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket with no partial file for the target name
        # When querying upload-status
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/upload-status", params={"filename": "fresh.bin"}
        )

        # Then the status is "new" with a zero offset
        assert r.status_code == 200
        assert r.json()["status"] == "new"
        assert r.json()["offset"] == 0

    def test_partial_file_reports_resumable_offset(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a partial upload (size declared, only part of the bytes sent)
        import hashlib

        full = b"abcdefgh" * 4  # 32 bytes
        first = full[:12]
        sha = hashlib.sha256(full).hexdigest()
        bb_upload(bb_http, bb_bucket, "part.bin", first, sha256sum=sha, size=len(full))

        # When querying upload-status for that file
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/upload-status",
            params={"filename": "part.bin", "sha256sum": sha, "size": len(full)},
        )

        # Then the status is "resumable" from the received offset
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "resumable"
        assert body["offset"] == len(first)

    def test_complete_file_reports_complete_status(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a fully uploaded file
        import hashlib

        content = b"fully here"
        sha = hashlib.sha256(content).hexdigest()
        bb_upload(bb_http, bb_bucket, "done.bin", content, sha256sum=sha)

        # When querying upload-status with the matching checksum
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/upload-status",
            params={"filename": "done.bin", "sha256sum": sha, "size": len(content)},
        )

        # Then the status is "complete"
        assert r.status_code == 200
        assert r.json()["status"] == "complete"
