# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box, use-case-driven tests for the bucket lifecycle.

Drives the *real* hsync-server over loopback HTTP only (no internal imports,
no mocks). A test passes as long as the observable HTTP behaviour is correct,
regardless of internal refactoring.
"""

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("bb_http")


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
        r = bb_http.post("/api/v1/buckets", params={"bucket": "bb-create-payload"})

        # Then the response confirms creation with the expected shape
        assert r.status_code == 201
        assert r.json()["info"] == "Bucket created successfully"
        # Cleanup
        bb_http.delete("/api/v1/buckets/bb-create-payload")

    def test_name_with_hyphens_and_underscores_accepted(self, bb_http: httpx.Client):
        # Given a valid name containing hyphens and underscores
        name = "bb-valid-name_1"

        # When creating the bucket
        r = bb_http.post("/api/v1/buckets", params={"bucket": name})

        # Then it is accepted
        assert r.status_code == 201
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
        assert r.status_code == 201, (
            f"expected 201 for {len(max_name)}-char name, got {r.status_code}"
        )
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
