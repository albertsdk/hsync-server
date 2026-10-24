# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the file upload use case.

Drives the real server over HTTP only. Verifies observable behaviour
(listing, checksums, round-trip, resume, compression, rejections) without
touching any internal module.
"""

import hashlib
import os

import httpx
import pytest
import zstandard as zstd

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]


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
            params={"filename": "rt.bin", "cid": "bbclient0001bbcltBB10"},
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

    def test_permission_only_upload_updates_permissions(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given an uploaded file
        content = b"same bytes"
        r1 = bb_upload(bb_http, bb_bucket, "perm.bin", content, permissions="644")
        assert r1.status_code == 201

        # When uploading the identical content with different permissions
        r2 = bb_upload(bb_http, bb_bucket, "perm.bin", content, permissions="600")

        # Then the server applies the new permissions (200, no byte rewrite)
        assert r2.status_code == 200
        assert r2.json()["info"] == "Permissions updated"

        # And the listing reflects the new permissions
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        match = [e for e in listed if e["name"] == "perm.bin"]
        assert len(match) == 1
        assert match[0]["permissions"] == "600"

    def test_patch_permissions_endpoint_updates_file_without_payload(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """The public metadata-only PATCH endpoint updates permissions without
        an upload payload, and the change is visible in the listing."""
        # Given an uploaded file
        content = b"patch me"
        r1 = bb_upload(bb_http, bb_bucket, "patch.bin", content, permissions="644")
        assert r1.status_code == 201

        # When the permissions are patched through the dedicated endpoint
        r2 = bb_http.patch(
            f"/api/v1/buckets/{bb_bucket}/files/permissions",
            params={"filename": "patch.bin", "permissions": "600"},
        )

        # Then the server applies the new mode (200, no file payload sent);
        # the PATCH endpoint reports its metadata-only update message (the
        # permission-only *upload* path above uses "Permissions updated").
        assert r2.status_code == 200
        assert r2.json()["info"] == "Metadata updated"

        # And the listing reflects the new permissions
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        match = [e for e in listed if e["name"] == "patch.bin"]
        assert len(match) == 1
        assert match[0]["permissions"] == "600"

        # And the content is untouched
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "patch.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == content

    def test_list_dirs_endpoint_returns_directory_permissions(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """The public /dirs endpoint lists directory permissions for the
        download-only directory-permission converge."""
        # Given a file in a nested path (the upload creates the directories)
        r1 = bb_upload(bb_http, bb_bucket, "nested.bin", b"x", path="sub/deep", permissions="644")
        assert r1.status_code == 201

        # When the directory metadata is listed
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/dirs").json()

        # Then every directory under the bucket is reported with its mode
        paths = {e["path"]: e["permissions"] for e in listed}
        assert "sub" in paths
        assert "sub/deep" in paths
        assert isinstance(paths["sub/deep"], str)

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

        # And the file must still be intact — a duplicate rejection must never
        # corrupt the existing file content on disk
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "dup.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == content


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
    """Causal upload arbitration over the public wire contract (ADR-0022).

    These black-box tests drive only the HTTP surface: the ``vclock`` form
    field is the client's causal history, the ``resolution`` response field and
    the ``superseded``/``conflict_lost`` 409 tokens are the observable outcome.
    The wall clock is never part of the contract.
    """

    @staticmethod
    def _vclock(cid: str, counter: int) -> str:
        import json

        return json.dumps({cid: counter})

    @staticmethod
    def _conflicted_copies(bb_http: httpx.Client, bb_bucket: str, stem: str) -> list[dict]:
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        return [e for e in listed if "conflicted_copy" in e["name"] and stem in e["name"]]

    def test_dominating_version_accepted_without_copy(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """A causally-ahead upload (same author, higher counter) replaces the
        content silently: 201 ``resolution=accepted``, no conflicted copy."""
        cid = "TestClientA1234567890"
        old = b"version one data"
        new = b"version two - updated content"

        bb_upload(bb_http, bb_bucket, "over.bin", old, vclock=self._vclock(cid, 1))
        r = bb_upload(bb_http, bb_bucket, "over.bin", new, vclock=self._vclock(cid, 2))

        assert r.status_code == 201
        assert r.json()["resolution"] == "accepted"

        # Main path has the new content...
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "over.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == new
        # ...and no conflicted copy is created: this was a clean causal edit.
        assert self._conflicted_copies(bb_http, bb_bucket, "over") == []

    def test_stale_version_superseded_without_copy(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """A causally-dominated upload (older content, older clock) is rejected
        with 409 ``superseded``: no copy is created because the ancestor's data
        is already inside the winner's history."""
        cid = "TestClientA1234567890"
        new = b"version two - updated content"
        old = b"version one data"

        # First push the causally-newer version...
        r1 = bb_upload(bb_http, bb_bucket, "stale.bin", new, vclock=self._vclock(cid, 2))
        assert r1.status_code == 201

        # ...then a stale peer re-pushes the older content with the older clock.
        r2 = bb_upload(bb_http, bb_bucket, "stale.bin", old, vclock=self._vclock(cid, 1))

        assert r2.status_code == 409
        assert r2.json()["resolution"] == "superseded"

        # The winner remains on disk and no conflicted copy exists.
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "stale.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == new
        assert self._conflicted_copies(bb_http, bb_bucket, "stale") == []

    def test_concurrent_tie_break_deterministic_and_preserves_loser(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        """True divergence (concurrent clocks from two authors) is resolved by
        the deterministic SHA-256 tie-break. The incoming content either wins
        (201 ``accepted_conflict_won``, previous preserved) or loses
        (409 ``conflict_lost``, incoming preserved) — in both cases exactly one
        conflicted copy survives and the main path keeps the winner."""
        cid_a = "TestClientA1234567890"
        cid_b = "TestClientB1234567890"
        old = b"version one data"
        new = b"version two - updated content"

        # Compute the deterministic winner up front (highest SHA-256 wins).
        win, lose = sorted((old, new), key=_sha, reverse=True)

        bb_upload(bb_http, bb_bucket, "tie.bin", old, vclock=self._vclock(cid_a, 1))
        r = bb_upload(bb_http, bb_bucket, "tie.bin", new, vclock=self._vclock(cid_b, 1))

        copies = self._conflicted_copies(bb_http, bb_bucket, "tie")
        if new == win:
            # Incoming wins the tie-break → accepted, previous preserved.
            assert r.status_code == 201
            assert r.json()["resolution"] == "accepted_conflict_won"
            assert len(copies) == 1, f"expected 1 conflicted copy, got {len(copies)}"
            copy_dl = bb_http.get(
                f"/api/v1/buckets/{bb_bucket}/files/download",
                params={"filename": copies[0]["filepath"]},
            )
            assert copy_dl.status_code == 200
            assert copy_dl.content == old
        else:
            # Incoming loses → rejected, incoming bytes preserved in a copy.
            assert r.status_code == 409
            assert r.json()["resolution"] == "conflict_lost"
            assert len(copies) == 1, f"expected 1 conflicted copy, got {len(copies)}"
            copy_dl = bb_http.get(
                f"/api/v1/buckets/{bb_bucket}/files/download",
                params={"filename": copies[0]["filepath"]},
            )
            assert copy_dl.status_code == 200
            assert copy_dl.content == new

        # The main path always keeps the deterministic winner.
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download", params={"filename": "tie.bin"}
        )
        assert dl.status_code == 200
        assert dl.content == win

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
            data={"cid": "bbclient0001bbcltBB10", "path": "", "permissions": "644"},
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
                        "cid": "bbclient0001bbcltBB10",
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

    def test_upload_too_short_cid_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a CID with only 12 characters (valid under old {1,64}, invalid under {21})
        # When uploading with that CID
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/files",
            data={
                "cid": "tooshort12345",
                "path": "",
                "sha256sum": _sha(b"x"),
                "permissions": "644",
                "offset": "0",
                "size": "0",
                "compressed": "false",
            },
            files={"file": ("x.bin", b"x", "application/octet-stream")},
        )

        # Then the upload is rejected with a validation error
        assert r.status_code == 400

    def test_upload_too_long_cid_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a CID with 64 characters (valid under old {1,64}, invalid under {21})
        # When uploading with that CID
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/files",
            data={
                "cid": "A" * 64,
                "path": "",
                "sha256sum": _sha(b"x"),
                "permissions": "644",
                "offset": "0",
                "size": "0",
                "compressed": "false",
            },
            files={"file": ("x.bin", b"x", "application/octet-stream")},
        )

        # Then the upload is rejected with a validation error
        assert r.status_code == 400


class TestUploadFilesystemPathLimits:
    """Filesystem byte limits: 255-byte path components, overlong names."""

    def test_round_trip_at_component_limit(self, bb_upload, bb_http: httpx.Client, bb_bucket: str):
        # Given a directory and a filename both exactly at the 255-byte limit
        dirname = "d" * 255
        filename = "f" * 255
        content = b"stored at the component limit"

        # When uploading a file at that path
        r = bb_upload(bb_http, bb_bucket, filename, content, path=dirname)

        # Then it is listed under the full path and round-trips byte-for-byte
        assert r.status_code == 201, r.text
        rel = f"{dirname}/{filename}"
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        entries = [entry for entry in listed if entry["filepath"] == rel]
        assert len(entries) == 1
        assert entries[0]["size"] == len(content)
        assert entries[0]["sha256sum"] == _sha(content)
        dl = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/download",
            params={"filename": rel},
            headers={"Accept-Encoding": "identity"},
        )
        assert dl.status_code == 200, dl.text
        assert dl.content == content

    def test_overlong_filename_sanitized_to_component_limit(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a 300-byte ASCII filename (past the 255-byte component limit)
        content = b"name truncated"

        # When uploading it
        r = bb_upload(bb_http, bb_bucket, "a" * 300, content)

        # Then the stored name is the 255-byte sanitized form and the content
        # is intact (truncation must never drop or corrupt the payload)
        assert r.status_code == 201, r.text
        listed = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
        assert len(listed) == 1
        assert listed[0]["filepath"] == "a" * 255
        assert listed[0]["sha256sum"] == _sha(content)

    def test_overlong_path_component_rejected(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a directory component one byte over the 255-byte limit
        # When uploading into it
        r = bb_upload(bb_http, bb_bucket, "x.bin", b"rejected", path="p" * 256)

        # Then the request is rejected as an invalid path and nothing is stored
        assert r.status_code == 400
        assert bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json() == []


class TestDiskSpaceGuard:
    def test_upload_refused_with_507_when_reserve_unreachable(self, start_live_server, bb_upload):
        # Given a server whose free-space reserve exceeds any real disk (1 PiB).
        # The value carries a unit: operator-facing settings reject a bare byte
        # count so `500` cannot be silently read as 500 bytes.
        with start_live_server({"HSYNC_MIN_FREE_DISK_SPACE": "1024TB"}) as (base, api_key):
            with httpx.Client(
                base_url=base, headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0
            ) as c:
                bucket = "bbspspace"
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201

                # When uploading any file
                r = bb_upload(c, bucket, "x.bin", b"x")

                # Then the server refuses the write with 507 before storing it
                assert r.status_code == 507, r.text
                assert c.get(f"/api/v1/buckets/{bucket}/files").json() == []


class TestCompressedUploadBombGuard:
    def test_payload_expanding_past_declared_size_is_refused(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str
    ):
        # Given a highly compressible payload (a decompression-bomb shape)
        original = b"\x00" * (5 * 1024 * 1024)
        compressed = zstd.ZstdCompressor().compress(original)
        assert len(compressed) < len(original)

        # When uploading it compressed but declaring a far smaller size
        r = bb_upload(
            bb_http,
            bb_bucket,
            "bomb.bin",
            compressed,
            sha256sum=_sha(original),
            size=1024,
            compressed="true",
        )

        # Then the server stops expansion at the declared cap and rejects it
        assert r.status_code == 400, r.text

        # And nothing is stored
        assert bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json() == []
