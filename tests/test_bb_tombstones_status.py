# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for tombstone listing and resumable upload-status.

Drives the real server over HTTP only. Verifies observable behaviour of the
deletion-marker and resume-status endpoints without touching internals.
"""

import hashlib
import os
import time
import uuid

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


class TestTombstoneMerge:
    """POST /api/v1/buckets/{bucket}/tombstones — causal merge endpoint."""

    def test_valid_merge_returns_merged_count(
        self, bb_http: httpx.Client, bb_bucket: str, bb_sample
    ):
        # Given a tombstone entry for a file that was deleted on a peer
        s = bb_sample(prefix="tmb", size=0)
        content = b"deleted elsewhere"
        key = hashlib.sha256(s.name.encode()).hexdigest()
        entry = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "version_id": "v1-delete",
            "origin_cid": _CID,
            "ts": int(time.time()),
            "vclock": {_CID: 1},
        }

        # When posting the merge payload
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/tombstones",
            json={key: entry},
        )

        # Then the server accepts it and reports the merge count
        assert r.status_code == 200, f"{r.status_code} {r.text}. {s.ctx}"
        assert r.json()["merged"] >= 1

        # And the tombstone is now visible in the listing
        listing = bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones").json()
        assert key in listing, f"merged tombstone not listed. {s.ctx}"

    def test_invalid_json_body_returns_400(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a non-JSON body
        # When posting it
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/tombstones",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

        # Then the server rejects it
        assert r.status_code == 400

    def test_non_object_body_returns_400(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a JSON array (not an object)
        # When posting it
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/tombstones",
            json=[1, 2, 3],
        )

        # Then the server rejects it
        assert r.status_code == 400

    def test_tombstones_isolated_across_buckets(self, bb_http: httpx.Client, bb_bucket: str):
        # Given two distinct buckets
        other = f"bb-iso-{uuid.uuid4().hex[:8]}"
        r = bb_http.post("/api/v1/buckets", params={"bucket": other})
        assert r.status_code == 201, r.text
        try:
            # And a tombstone merged into bucket A only
            content = b"iso-delete"
            key = hashlib.sha256(b"iso-key").hexdigest()
            entry = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "version_id": "v-iso",
                "origin_cid": _CID,
                "ts": int(time.time()),
                "vclock": {_CID: 1},
            }
            r = bb_http.post(
                f"/api/v1/buckets/{bb_bucket}/tombstones",
                json={key: entry},
            )
            assert r.status_code == 200

            # When listing tombstones in bucket B
            other_listing = bb_http.get(f"/api/v1/buckets/{other}/tombstones").json()

            # Then bucket B has no tombstones from bucket A
            assert key not in other_listing, "tombstone leaked across buckets"
        finally:
            bb_http.delete(f"/api/v1/buckets/{other}")


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
