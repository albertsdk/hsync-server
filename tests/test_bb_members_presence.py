# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for bucket membership and the presence counters.

Drives the real server over HTTP + WebSocket. The membership API
(``/members``) and the connected-member presence counter had no BB coverage;
this file pins the public lifecycle: create a non-admin client, grant/revoke
membership, and observe the presence counters when the member holds a live
WebSocket.

Requires HSYNC_BB_SERVER=1 to run.
"""

import asyncio
import json
import os
import uuid

import httpx
import pytest
import websockets
from hsync_common.protocol.handshake import build_ws_auth_request

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]


def _cid(tag: str) -> str:
    """Build a wire-valid cid (exactly 21 alphanumeric chars) from a short tag."""
    base = f"bb{tag}"
    return base + "0" * (21 - len(base))


def _ws_base(live_server: tuple[str, str]) -> str:
    base, _ = live_server
    return base.replace("http://", "ws://")


class TestMembers:
    def test_grant_list_revoke_lifecycle(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a fresh bucket and a new non-admin client account
        name = f"member-{uuid.uuid4().hex[:8]}"
        created = bb_http.post("/api/v1/admin/clients", json={"name": name, "role": "client"})
        assert created.status_code == 201, created.text
        client_id = created.json()["id"]
        try:
            # When granting membership to that account
            granted = bb_http.post(
                f"/api/v1/buckets/{bb_bucket}/members", json={"client_id": client_id}
            )

            # Then it is accepted and the member appears in the listing
            assert granted.status_code == 201, granted.text
            assert granted.json()["role"] == "member"

            members = bb_http.get(f"/api/v1/buckets/{bb_bucket}/members")
            assert members.status_code == 200
            ids = {m["id"] for m in members.json()["members"]}
            assert client_id in ids

            # When revoking membership
            revoked = bb_http.delete(f"/api/v1/buckets/{bb_bucket}/members/{client_id}")

            # Then it is removed from the listing
            assert revoked.status_code == 200, revoked.text
            members = bb_http.get(f"/api/v1/buckets/{bb_bucket}/members")
            ids = {m["id"] for m in members.json()["members"]}
            assert client_id not in ids
        finally:
            # Clean up the account regardless of assertion outcome
            bb_http.delete(f"/api/v1/admin/clients/{client_id}")

    def test_grant_unknown_account_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket
        # When granting membership to a non-existent account
        r = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/members", json={"client_id": "no-such-account"}
        )

        # Then the server reports the account as not found
        assert r.status_code == 404

    def test_revoke_non_member_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket with no such member
        # When revoking it
        r = bb_http.delete(f"/api/v1/buckets/{bb_bucket}/members/no-such-account")

        # Then the server reports it as not a member
        assert r.status_code == 404


class TestPresenceWithMember:
    async def test_presence_counts_connected_member(
        self, bb_http: httpx.Client, bb_bucket: str, live_server: tuple[str, str]
    ):
        # Given a new non-admin client granted membership on the bucket
        name = f"member-{uuid.uuid4().hex[:8]}"
        created = bb_http.post("/api/v1/admin/clients", json={"name": name, "role": "client"})
        assert created.status_code == 201, created.text
        client_id = created.json()["id"]
        member_token = created.json()["token"]
        try:
            granted = bb_http.post(
                f"/api/v1/buckets/{bb_bucket}/members", json={"client_id": client_id}
            )
            assert granted.status_code == 201, granted.text

            # Before any WS, the member is counted but not connected
            pres = bb_http.get(f"/api/v1/buckets/{bb_bucket}/presence")
            assert pres.status_code == 200
            assert pres.json()["total"] == 1
            assert pres.json()["connected"] == 0

            # When the member holds a live WebSocket on the bucket
            base_url, api_key = live_server
            ws = await websockets.connect(
                f"{_ws_base(live_server)}/api/v1/buckets/{bb_bucket}/events/{_cid('mem')}",
                max_size=None,
            )
            try:
                frame = build_ws_auth_request(
                    token=member_token, app_version="bb-test", capabilities=[]
                )
                await ws.send(json.dumps(frame))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert reply["type"] == "auth"
                assert reply["status"] == "ok"

                # Then presence reports the connected member (admins excluded)
                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0,
                ) as ac:
                    deadline = asyncio.get_event_loop().time() + 10
                    body = None
                    while True:
                        r = await ac.get(f"/api/v1/buckets/{bb_bucket}/presence")
                        body = r.json()
                        if body.get("connected") == 1:
                            break
                        if asyncio.get_event_loop().time() > deadline:
                            pytest.fail(f"presence never reported a connected member: {body}")
                        await asyncio.sleep(0.2)
                    assert body["total"] == 1
                    assert body["connected"] == 1
            finally:
                await ws.close()
        finally:
            bb_http.delete(f"/api/v1/buckets/{bb_bucket}/members/{client_id}")
            bb_http.delete(f"/api/v1/admin/clients/{client_id}")


class TestAdminCreateBucketOnBehalf:
    """Public contract of ``POST /api/v1/admin/buckets`` (create-on-behalf)."""

    @staticmethod
    def _admin_id(bb_http: httpx.Client) -> str:
        r = bb_http.get("/api/v1/admin/clients")
        assert r.status_code == 200, r.text
        return next(a["id"] for a in r.json()["clients"] if a["role"] == "admin")

    def test_create_on_behalf_and_duplicate_conflict(self, bb_http: httpx.Client):
        # Given the admin account id and a fresh bucket name
        admin_id = self._admin_id(bb_http)
        bucket = f"bb-onbehalf-{uuid.uuid4().hex[:8]}"
        try:
            # When creating the bucket on behalf of the admin
            created = bb_http.post(
                "/api/v1/admin/buckets", json={"bucket": bucket, "owner": admin_id}
            )

            # Then it is created and visible in the bucket inventory
            assert created.status_code == 201, created.text
            listed = bb_http.get("/api/v1/admin/buckets")
            assert listed.status_code == 200, listed.text
            assert bucket in {b["name"] for b in listed.json()["buckets"]}

            # When creating the same bucket again
            duplicate = bb_http.post(
                "/api/v1/admin/buckets", json={"bucket": bucket, "owner": admin_id}
            )

            # Then the server reports the conflict with the documented code
            assert duplicate.status_code == 409
            assert duplicate.json()["error"] == "HSYNC-3004"
        finally:
            bb_http.delete(f"/api/v1/buckets/{bucket}")

    def test_create_on_behalf_invalid_name_rejected(self, bb_http: httpx.Client):
        # Given an invalid bucket name and the admin account id
        admin_id = self._admin_id(bb_http)

        # When creating it on behalf
        r = bb_http.post("/api/v1/admin/buckets", json={"bucket": "../escape", "owner": admin_id})

        # Then it is rejected as a bad name
        assert r.status_code == 400

    def test_create_on_behalf_unknown_owner_returns_404(self, bb_http: httpx.Client):
        # Given an unknown owner account
        # When creating a bucket on its behalf
        r = bb_http.post(
            "/api/v1/admin/buckets",
            json={"bucket": f"bb-ghost-{uuid.uuid4().hex[:8]}", "owner": "no-such-account"},
        )

        # Then the owner is reported as not found
        assert r.status_code == 404
