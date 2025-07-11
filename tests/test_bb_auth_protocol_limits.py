# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for authentication, protocol-version and rate-limit use cases.

Drives the real server over HTTP only. Verifies the public auth/protocol/limit
behaviour without touching internals.
"""

import httpx
import pytest
from hsync_common.protocol.handshake import PROTOCOL_VERSION_REST

pytestmark = pytest.mark.usefixtures("bb_http")


class TestAuthAndProtocol:
    def test_valid_bearer_succeeds(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a valid API key and an existing bucket
        # When querying the bucket
        r = bb_http.get(f"/api/v1/buckets/{bb_bucket}")

        # Then the request is authorized and returns the bucket info
        assert r.status_code == 200
        assert r.json()["name"] == bb_bucket

    def test_protocol_version_mismatch_rejected(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a client advertising an incompatible REST protocol version
        # When querying files
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files",
            headers={"X-Protocol-Version": "api/999"},
        )

        # Then the server refuses with 426 and advertises its own version
        assert r.status_code == 426
        assert r.headers.get("X-Protocol-Version") == PROTOCOL_VERSION_REST

    def test_health_without_auth(self, live_server: tuple[str, str]):
        # Given a client with no API key
        base, _ = live_server
        # When hitting the health endpoint
        r = httpx.get(f"{base}/health", timeout=10.0)

        # Then it succeeds without authentication
        assert r.status_code == 200

    def test_time_endpoint_returns_timestamp(self, bb_http: httpx.Client):
        # Given an authenticated client
        # When requesting the server time
        r = bb_http.get("/api/v1/time")

        # Then a numeric timestamp is returned
        assert r.status_code == 200
        ts = r.json()["timestamp"]
        assert isinstance(ts, (int, float))
        assert ts > 0


class TestRateLimits:
    def test_upload_rate_limit_returns_429(self, start_live_server, bb_upload):
        # Given a server with a tiny upload rate limit
        with start_live_server({"HSYNC_RATE_LIMIT_UPLOAD": "2/minute"}) as (base, api_key):
            auth = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(base_url=base, headers=auth, timeout=15.0) as c:
                bucket = "bbrl"
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201

                # When uploading more than the allowed number of times
                codes = []
                for _ in range(4):
                    r = bb_upload(c, bucket, "f.bin", b"x")
                    codes.append(r.status_code)

                # Then at least one upload is throttled with 429
                assert 429 in codes
                assert 201 in codes  # the first uploads succeeded

    def test_download_rate_limit_returns_429(self, start_live_server, bb_upload):
        # Given a server with a tiny download rate limit and one uploaded file
        with start_live_server({"HSYNC_RATE_LIMIT_DOWNLOAD": "2/minute"}) as (base, api_key):
            auth = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(base_url=base, headers=auth, timeout=15.0) as c:
                bucket = "bbrldl"
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201
                assert bb_upload(c, bucket, "f.bin", b"x").status_code == 201

                # When downloading more than the allowed number of times
                codes = []
                for _ in range(4):
                    r = c.get(
                        f"/api/v1/buckets/{bucket}/files/download", params={"filename": "f.bin"}
                    )
                    codes.append(r.status_code)

                # Then at least one download is throttled with 429
                assert 429 in codes
                assert 200 in codes  # the first downloads succeeded
