# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the file upload use case.

Drives the real server over HTTP only. Verifies observable behaviour
(listing, checksums, round-trip, resume, compression, rejections) without
touching any internal module.
"""

import hashlib

import httpx
import pytest
import zstandard as zstd

pytestmark = pytest.mark.usefixtures("bb_http")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestUploadHappy:
    def test_upload_then_listed_with_correct_hash(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an empty bucket and some content
        content = b"hello black-box upload\n"

        # When uploading it
        r = bb_upload(bb_http, bb_bucket, "note.txt", content)

        # Then the upload succeeds and the file appears in the listing with the
        # expected size and checksum
        assert r.status_code == 201
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        assert len(listed) == 1
        entry = listed[0]
        assert entry["name"] == "note.txt"
        assert entry["size"] == len(content)
        assert entry["sha256sum"] == _sha(content)

    def test_upload_then_download_round_trips(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an uploaded file (small, so download is not compressed)
        content = b"round trip payload" * 1

        # When downloading it back
        bb_upload(bb_http, bb_bucket, "rt.bin", content)
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": "rt.bin", "cid": "bbclient0001"},
        )

        # Then the downloaded bytes match the original and a checksum header is set
        assert dl.status_code == 200
        assert dl.content == content
        assert dl.headers.get("X-Checksum-SHA256") == _sha(content)

    def test_upload_into_nested_subdir(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given content destined for a deeply nested path
        content = b"nested"

        # When uploading with a subdir path
        r = bb_upload(bb_http, bb_bucket, "deep.bin", content, path="a/b/c")

        # Then it succeeds and is listed with the nested relative path
        assert r.status_code == 201
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        match = [e for e in listed if e["name"] == "deep.bin"]
        assert len(match) == 1
        assert "a/b/c" in match[0]["filepath"]
        # And the file is downloadable through its nested path
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "a/b/c/deep.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == content

    def test_duplicate_identical_upload_returns_conflict(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an already-uploaded file
        content = b"same bytes"
        bb_upload(bb_http, bb_bucket, "dup.bin", content)

        # When uploading the identical content again
        r = bb_upload(bb_http, bb_bucket, "dup.bin", content)

        # Then the server reports it is already present (no rewrite)
        assert r.status_code == 409
        assert r.json()["info"] == "File already present"


class TestUploadResumable:
    def test_partial_then_status_then_resume_completes(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a file split into two chunks
        full = b"01234567"
        first, rest = full[:4], full[4:]
        sha = _sha(full)

        # When sending the first chunk as a partial upload
        r1 = bb_upload(bb_http, bb_bucket, "res.bin", first, sha256sum=sha, size=len(full))

        # Then the server acknowledges the partial with the received offset
        assert r1.status_code == 202
        assert r1.json()["offset"] == len(first)

        # And the upload-status endpoint reports it as resumable from that offset
        status = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/upload-status",
            params={"filename": "res.bin", "sha256sum": sha, "size": len(full)},
        )
        assert status.status_code == 200
        assert status.json()["status"] == "resumable"
        assert status.json()["offset"] == len(first)

        # When resuming with the remaining bytes
        r2 = bb_upload(
            bb_http, bb_bucket, "res.bin", rest, sha256sum=sha, offset=len(first), size=len(full)
        )

        # Then the upload completes and the downloaded content matches the original
        assert r2.status_code == 201
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "res.bin"}
        )
        assert dl.content == full


class TestUploadCompressed:
    def test_compressed_payload_decompresses_to_declared_size(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a compressible payload and its zstd-compressed form
        original = b"compressible content " * 200
        sha = _sha(original)
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original)

        # When uploading it as a compressed upload (size = decompressed size)
        r = bb_upload(
            bb_http,
            bb_bucket,
            "comp.bin",
            compressed,
            sha256sum=sha,
            size=len(original),
            compressed="true",
        )

        # Then the upload succeeds and the stored file decompresses to the original
        assert r.status_code == 201
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "comp.bin"}
        )
        assert dl.content == original


class TestUploadOverwrite:
    def test_overwrite_with_correct_checksum_replaces_content(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """Uploading a new version of an existing file must replace its content."""
        old = b"version one data"
        new = b"version two - updated content"
        bb_upload(bb_http, bb_bucket, "over.bin", old)

        # Overwrite with new content
        r = bb_upload(bb_http, bb_bucket, "over.bin", new)
        assert r.status_code == 201

        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "over.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == new
        assert dl.content != old

    def test_overwrite_with_wrong_checksum_preserves_original(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """When an overwrite upload fails due to a checksum mismatch, the original
        file must remain intact — the new content must never be visible."""
        original = b"precious original data"
        bb_upload(bb_http, bb_bucket, "precious.bin", original)
        original_sha = hashlib.sha256(original).hexdigest()

        # Attempt overwrite with wrong checksum
        new_content = b"corrupted overwrite attempt"
        bad_sha = "0" * 64
        r = bb_upload(bb_http, bb_bucket, "precious.bin", new_content, sha256sum=bad_sha)
        assert r.status_code == 400

        # The original file must still be downloadable with its original content
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "precious.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == original
        assert dl.headers.get("X-Checksum-SHA256") == original_sha

        # The file listing must still show the original metadata
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        match = [e for e in listed if e["name"] == "precious.bin"]
        assert len(match) == 1
        assert match[0]["size"] == len(original)
        assert match[0]["sha256sum"] == original_sha


class TestUploadNegative:
    def test_wrong_checksum_rejected(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given content and a checksum that does not match it
        content = b"checksum me"
        bad_sha = "0" * 64

        # When uploading with the wrong checksum
        r = bb_upload(bb_http, bb_bucket, "bad.bin", content, sha256sum=bad_sha)

        # Then the upload is rejected
        assert r.status_code == 400

    def test_missing_checksum_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a multipart request without the sha256sum field
        # When uploading
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/files",
            data={"cid": "bbclient0001", "path": "", "permissions": "644"},
            files={"file": ("nobytes.bin", b"x", "application/octet-stream")},
        )

        # Then the server rejects the missing required field
        assert r.status_code == 422

    def test_path_traversal_rejected(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given a path that tries to escape into a sibling bucket
        content = b"escape"

        # When uploading with a traversal path
        r = bb_upload(bb_http, bb_bucket, "x.bin", content, path="../other-bucket")

        # Then the upload is rejected
        assert r.status_code == 400

    def test_internal_engine_filename_rejected(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a filename reserved for the sync engine
        content = b"internal"

        # When uploading it
        r = bb_upload(bb_http, bb_bucket, ".hsyncignore", content)

        # Then the upload is rejected
        assert r.status_code == 400

    def test_compressed_with_offset_rejected(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a compressed upload that also claims to resume
        original = b"abc"
        compressed = zstd.ZstdCompressor().compress(original)

        # When uploading compressed=true with offset>0
        r = bb_upload(
            bb_http,
            bb_bucket,
            "c.bin",
            compressed,
            sha256sum=_sha(original),
            size=len(original),
            compressed="true",
            offset=1,
        )

        # Then the server rejects the incompatible combination
        assert r.status_code == 400

    def test_upload_exceeds_max_size_rejected(self, start_live_server):
        # Given a server with a tiny upload cap
        with start_live_server({"HSYNC_MAX_UPLOAD_SIZE": "10"}) as (base, api_key):
            auth = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(base_url=base, headers=auth, timeout=15.0) as c:
                bucket = "bblimit"
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201

                # When uploading a file whose declared size exceeds the cap
                r = c.post(
                    f"/api/v1/buckets/{bucket}/files",
                    data={
                        "cid": "bbclient0001",
                        "path": "",
                        "sha256sum": _sha(b"x" * 100),
                        "permissions": "644",
                        "offset": "0",
                        "size": "100",
                        "compressed": "false",
                    },
                    files={"file": ("big.bin", b"x", "application/octet-stream")},
                )

                # Then the upload is rejected before any data is accepted
                assert r.status_code == 400
