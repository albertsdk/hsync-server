# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for file download and delete use cases.

Drives the real server over HTTP only. Verifies observable behaviour
(streamed bytes, checksum header, tombstone side-effects, rejections)
without touching any internal module.
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


def _delete(client: httpx.Client, bucket: str, filename: str, cid: str = _CID) -> httpx.Response:
    return client.request(
        "DELETE",
        f"/api/v1/buckets/{bucket}/files",
        data={"cid": cid, "filename": filename},
    )


class TestDownloadHappy:
    def test_download_streams_correct_bytes(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given an uploaded file (small enough to skip download compression)
        content = b"download me please"

        # When downloading it
        bb_upload(bb_http, bb_bucket, "dl.bin", content)
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "dl.bin"}
        )

        # Then the streamed bytes equal the original
        assert r.status_code == 200
        assert r.content == content

    def test_download_accepts_cid_param_without_behaviour_change(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an uploaded file
        content = b"with cid"

        # When downloading with a cid query parameter
        bb_upload(bb_http, bb_bucket, "c.bin", content)
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": "c.bin", "cid": _CID},
        )

        # Then the download still succeeds and returns the original bytes
        assert r.status_code == 200
        assert r.content == content


class TestDownloadNegative:
    def test_download_missing_file_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket with no files
        # When downloading a non-existent file
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "nope.bin"}
        )

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_download_traversal_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a filename that attempts to escape the bucket
        # When downloading it
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": "../../../etc/passwd"},
        )

        # Then the request is rejected
        assert r.status_code == 400


class TestDeleteHappy:
    def test_delete_removes_file(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given an uploaded file
        bb_upload(bb_http, bb_bucket, "gone.bin", b"delete me")

        # When deleting it
        r = _delete(bb_http, bb_bucket, "gone.bin")

        # Then deletion succeeds and the file is no longer listed or downloadable
        assert r.status_code == 200
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        assert all(e["name"] != "gone.bin" for e in listed)
        assert (
            bb_http.get(
                f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "gone.bin"}
            ).status_code
            == 404
        )

    def test_delete_records_tombstone(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given an uploaded file with known content
        content = b"soon gone"
        bb_upload(bb_http, bb_bucket, "tomb.bin", content)

        # When deleting it
        del_r = _delete(bb_http, bb_bucket, "tomb.bin")

        # Then deletion succeeds and a tombstone marker is recorded with the
        # correct content hash
        assert del_r.status_code == 200
        tombs = bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones")
        assert tombs.status_code == 200
        payload = tombs.json()
        expected_sha = hashlib.sha256(content).hexdigest()
        # Exactly one tombstone for the deleted file
        assert len(payload) == 1, f"expected 1 tombstone, got {len(payload)}: {payload}"
        entry = next(iter(payload.values()))
        assert "sha256" in entry
        assert "ts" in entry
        assert entry["sha256"] == expected_sha

    def test_reupload_clears_tombstone(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given a deleted file (tombstone present)
        bb_upload(bb_http, bb_bucket, "reuse.bin", b"first")
        assert _delete(bb_http, bb_bucket, "reuse.bin").status_code == 200
        tombs_after = bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones").json()
        assert len(tombs_after) == 1

        # When re-uploading the same path
        bb_upload(bb_http, bb_bucket, "reuse.bin", b"second")

        # Then the tombstone is cleared
        assert bb_http.get(f"/api/v1/buckets/{bb_bucket}/tombstones").json() == {}


class TestDeleteNegative:
    def test_delete_missing_file_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket with no files
        # When deleting a non-existent file
        r = _delete(bb_http, bb_bucket, "nope.bin")

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_delete_traversal_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a filename that attempts to escape the bucket
        # When deleting it
        r = _delete(bb_http, bb_bucket, "../../../etc/passwd")

        # Then the request is rejected
        assert r.status_code == 400

    def test_delete_in_missing_bucket_returns_404(self, bb_http: httpx.Client):
        # Given a bucket that does not exist
        # When deleting a file in it
        r = _delete(bb_http, "bb-no-such-bucket", "x.bin")

        # Then the server reports the bucket as not found
        assert r.status_code == 404


class TestDownloadRange:
    """HTTP Range resumable-download contract (the wire the client's resume uses)."""

    def test_range_download_returns_206_and_the_tail(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str, bb_sample
    ):
        # Given a random file uploaded to the bucket
        sample = bb_sample(suffix=".bin", size=4096)
        assert bb_upload(bb_http, bb_bucket, sample.name, sample.payload).status_code == 201
        offset = len(sample.payload) // 3

        # When downloading only the tail with a Range header
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": sample.name},
            headers={"Range": f"bytes={offset}-"},
        )

        # Then the server answers 206 with exactly the remaining bytes
        assert r.status_code == 206, r.text
        assert r.headers.get("Content-Range") == (
            f"bytes {offset}-{len(sample.payload) - 1}/{len(sample.payload)}"
        ), sample.ctx
        assert r.content == sample.payload[offset:], sample.ctx

    def test_unsatisfiable_range_returns_416(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str, bb_sample
    ):
        # Given a random file uploaded to the bucket
        sample = bb_sample(suffix=".bin", size=2048)
        assert bb_upload(bb_http, bb_bucket, sample.name, sample.payload).status_code == 201

        # When the range starts at EOF (no byte left to serve)
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": sample.name},
            headers={"Range": f"bytes={len(sample.payload)}-"},
        )

        # Then it is unsatisfiable: 416 with a "bytes */size" Content-Range
        assert r.status_code == 416, r.text
        assert r.headers.get("Content-Range") == f"bytes */{len(sample.payload)}", sample.ctx
