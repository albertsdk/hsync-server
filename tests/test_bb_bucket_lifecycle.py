# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box, use-case-driven tests for the bucket lifecycle.

Drives the *real* hsync-server over loopback HTTP only (no internal imports,
no mocks). A test passes as long as the observable HTTP behaviour is correct,
regardless of internal refactoring.

Requires HSYNC_BB_SERVER=1 to run.
"""

import os
import uuid

import httpx
import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.usefixtures("bb_http"),
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
]


class TestBucketLifecycle:
    """Create / read / list / delete a bucket end to end."""

    def test_create_then_exists_and_lists_empty(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a freshly created bucket (bb_bucket fixture)
        # When querying its info and file listing
        info = bb_http.get(f"/api/v1/buckets/{bb_bucket}")
        files = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files")

        # Then the bucket is visible and the file list is empty
        assert info.status_code == 200
        assert info.json()["name"] == bb_bucket
        assert files.status_code == 200
        assert files.json() == []

    def test_create_reports_success_payload(self, bb_http: httpx.Client):
        # Given no pre-existing bucket
        # When creating it
        name = "bb-create-payload"
        r = bb_http.post("/api/v1/buckets", params={"bucket": name})
        try:
            # Then the response confirms creation with the expected shape
            assert r.status_code == 201
            assert r.json()["info"] == "Bucket created successfully"
        finally:
            bb_http.delete(f"/api/v1/buckets/{name}")

    def test_name_with_hyphens_and_underscores_accepted(self, bb_http: httpx.Client):
        # Given a valid name containing hyphens and underscores
        name = "bb-valid-name_1"

        # When creating the bucket
        r = bb_http.post("/api/v1/buckets", params={"bucket": name})
        try:
            # Then it is accepted
            assert r.status_code == 201
        finally:
            bb_http.delete(f"/api/v1/buckets/{name}")

    def test_get_missing_bucket_returns_404(self, bb_http: httpx.Client):
        # Given a bucket that does not exist
        # When querying it
        r = bb_http.get("/api/v1/buckets/bb-no-such-bucket")

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_invalid_bucket_name_rejected(self, bb_http: httpx.Client):
        # Given a traversal-style invalid name
        # When creating it
        r = bb_http.post("/api/v1/buckets", params={"bucket": "../escape"})

        # Then the request is rejected as a bad name
        assert r.status_code == 400

    def test_name_too_long_rejected(self, bb_http: httpx.Client):
        """A bucket name longer than 63 characters must be rejected.

        The limit is enforced by ``MAX_BUCKET_NAME_LENGTH`` in
        ``hsync_common.path_validation``. A name that exceeds it must not
        create a directory on the server filesystem.
        """
        too_long = "a" * 64
        r = bb_http.post("/api/v1/buckets", params={"bucket": too_long})
        assert r.status_code == 400, (
            f"expected 400 for {len(too_long)}-char name, got {r.status_code}"
        )

    def test_name_exactly_max_length_accepted(self, bb_http: httpx.Client):
        """A 63-character bucket name is accepted (the maximum allowed)."""
        max_name = "a" * 63
        r = bb_http.post("/api/v1/buckets", params={"bucket": max_name})
        try:
            assert r.status_code == 201, (
                f"expected 201 for {len(max_name)}-char name, got {r.status_code}"
            )
        finally:
            bb_http.delete(f"/api/v1/buckets/{max_name}")

    def test_leading_hyphen_name_rejected(self, bb_http: httpx.Client):
        # Given a name starting with a hyphen (forbidden by the contract)
        # When creating it
        r = bb_http.post("/api/v1/buckets", params={"bucket": "-bad"})

        # Then the request is rejected
        assert r.status_code == 400

    def test_duplicate_create_returns_conflict(self, bb_http: httpx.Client, bb_bucket: str):
        # Given an already-existing bucket (bb_bucket)
        # When creating it again
        r = bb_http.post("/api/v1/buckets", params={"bucket": bb_bucket})

        # Then the server reports a conflict
        assert r.status_code == 409
        assert r.json()["info"] == "Bucket already exists"

    def test_delete_removes_bucket(self, bb_http: httpx.Client, bb_bucket: str):
        # Given an existing bucket
        # When deleting it
        r = bb_http.delete(f"/api/v1/buckets/{bb_bucket}")

        # Then deletion succeeds and the bucket is no longer found
        assert r.status_code == 200
        assert r.json()["info"] == "Bucket deleted successfully"
        assert bb_http.get(f"/api/v1/buckets/{bb_bucket}").status_code == 404

    def test_delete_missing_bucket_returns_404(self, bb_http: httpx.Client):
        # Given a bucket that does not exist
        # When deleting it
        r = bb_http.delete("/api/v1/buckets/bb-no-such-bucket")

        # Then the server reports it as not found
        assert r.status_code == 404


class TestBucketAuth:
    """Authentication is enforced on every bucket endpoint."""

    def test_no_auth_returns_unauthorized(self, live_server: tuple[str, str]):
        # Given a client with no API key
        base_url, _ = live_server
        no_auth = httpx.Client(base_url=base_url, timeout=10.0)
        try:
            # When hitting a protected bucket endpoint
            r = no_auth.get("/api/v1/buckets/anything")
            # Then access is refused
            assert r.status_code in (401, 403)
        finally:
            no_auth.close()

    def test_wrong_key_returns_unauthorized(self, live_server: tuple[str, str]):
        # Given a client with a wrong API key
        base_url, _ = live_server
        wrong = httpx.Client(
            base_url=base_url, headers={"Authorization": "Bearer wrong-key"}, timeout=10.0
        )
        try:
            # When hitting a protected endpoint
            r = wrong.get("/api/v1/buckets/anything")
            # Then access is refused
            assert r.status_code in (401, 403)
        finally:
            wrong.close()


class TestBucketIsolation:
    """Files and tombstones in one bucket never surface in another."""

    def test_files_isolated_across_buckets(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str, bb_sample
    ):
        # Given two distinct buckets, each with a file the other does not have
        other = f"bb-iso-{uuid.uuid4().hex[:8]}"
        assert bb_http.post("/api/v1/buckets", params={"bucket": other}).status_code == 201
        try:
            s_a = bb_sample(prefix="fileA", size=0)
            s_b = bb_sample(prefix="fileB", size=0)
            bb_upload(bb_http, bb_bucket, s_a.name, b"in-A")
            bb_upload(bb_http, other, s_b.name, b"in-B")

            # When listing files in each bucket
            files_a = {
                f["filepath"] for f in bb_http.get(f"/api/v1/buckets/{bb_bucket}/files").json()
            }
            files_b = {f["filepath"] for f in bb_http.get(f"/api/v1/buckets/{other}/files").json()}

            # Then each bucket only sees its own files
            assert s_a.name in files_a and s_b.name not in files_a, f"leak A. {s_a.ctx} {s_b.ctx}"
            assert s_b.name in files_b and s_a.name not in files_b, f"leak B. {s_a.ctx} {s_b.ctx}"
        finally:
            bb_http.delete(f"/api/v1/buckets/{other}")


class TestPresence:
    """GET /api/v1/buckets/{bucket}/presence — connected-client count."""

    def test_fresh_bucket_reports_zero_connected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a freshly created bucket with no WebSocket clients
        # When querying presence
        r = bb_http.get(f"/api/v1/buckets/{bb_bucket}/presence")

        # Then the response reports zero connected WS clients and zero
        # non-admin members (the admin caller is excluded from both counters)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] == 0
        assert body["total"] == 0, "fresh admin-created bucket has no non-admin members"

    def test_presence_on_missing_bucket_returns_404(self, bb_http: httpx.Client):
        # Given a bucket that does not exist
        # When querying presence
        r = bb_http.get("/api/v1/buckets/bb-no-such-presence/presence")

        # Then the server reports it as not found
        assert r.status_code == 404


class TestStatePersistence:
    """Bucket, file and tombstone state must survive a server restart."""

    def test_bucket_files_and_tombstones_survive_restart(
        self, restartable_live_server, bb_upload, bb_sample
    ):
        bucket = f"bb-persist-{uuid.uuid4().hex[:8]}"
        keep = bb_sample(suffix=".bin", size=1024, prefix="keep")
        gone = bb_sample(suffix=".bin", size=64, prefix="gone")
        auth_cid = "bbclient0001bbcltBB10"

        # Given a bucket holding one live file and one deleted (tombstoned) file
        with restartable_live_server() as (base, api_key):
            with httpx.Client(
                base_url=base, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
            ) as c:
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201
                assert bb_upload(c, bucket, keep.name, keep.payload).status_code == 201
                assert bb_upload(c, bucket, gone.name, gone.payload).status_code == 201
                assert (
                    c.request(
                        "DELETE",
                        f"/api/v1/buckets/{bucket}/files",
                        data={"cid": auth_cid, "filename": gone.name},
                    ).status_code
                    == 200
                )

        # When the server is restarted on the same storage
        with restartable_live_server() as (base2, api_key2):
            with httpx.Client(
                base_url=base2, headers={"Authorization": f"Bearer {api_key2}"}, timeout=30.0
            ) as c2:
                # Then the bucket still exists, the live file survives with its
                # exact bytes, and the deleted file stays gone (tombstone held)
                assert c2.get(f"/api/v1/buckets/{bucket}").status_code == 200
                names = {e["name"] for e in c2.get(f"/api/v1/buckets/{bucket}/files").json()}
                assert keep.name in names, keep.ctx
                assert gone.name not in names, gone.ctx

                dl = c2.get(
                    f"/api/v1/buckets/{bucket}/files/download", params={"filename": keep.name}
                )
                assert dl.status_code == 200
                assert dl.content == keep.payload, keep.ctx
