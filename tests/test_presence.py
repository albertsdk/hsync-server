# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""Tests for bucket presence counters and the GET /presence endpoint."""

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from hsync_server.api.connection_manager import BucketConnectionManager, ConnectionEntry
from hsync_server.auth_store import get_auth_store
from tests.conftest import TEST_TOKEN


class TestPresenceCounts:
    """Unit tests for BucketConnectionManager.presence_counts."""

    def test_connected_is_intersection_of_connections_and_members(self):
        """Only connections belonging to non-admin members count as connected."""
        bcm = BucketConnectionManager()
        manager = bcm.get_manager("bucket1")
        manager.active_connections = {
            "cid-member": ConnectionEntry(
                websocket=MagicMock(), account_id="member1", key_generation=1, bucket="bucket1"
            ),
            # A live socket for an account that is NOT a share member
            # (e.g. admin) must not be counted as connected.
            "cid-other": ConnectionEntry(
                websocket=MagicMock(), account_id="admin1", key_generation=1, bucket="bucket1"
            ),
        }
        mock_store = MagicMock()
        mock_store.get_share_member_ids.return_value = {"member1", "member2"}

        with patch("hsync_server.api.connection_manager.get_auth_store", return_value=mock_store):
            connected, total = bcm.presence_counts("bucket1")

        assert (connected, total) == (1, 2)

    def test_no_manager_yields_zero_connected(self):
        """A bucket with no WS manager has no connected clients."""
        bcm = BucketConnectionManager()
        mock_store = MagicMock()
        mock_store.get_share_member_ids.return_value = {"member1"}

        with patch("hsync_server.api.connection_manager.get_auth_store", return_value=mock_store):
            connected, total = bcm.presence_counts("bucket1")

        assert (connected, total) == (0, 1)


class TestPresenceEndpoint:
    """Endpoint tests for GET /api/v1/buckets/{bucket}/presence."""

    @pytest.mark.asyncio
    async def test_presence_returns_membership_total_excluding_admins(self, test_app):
        """The total counts non-admin share members (admin is operator, not member).

        The bucket is created by the admin (who also becomes its owner), so an
        admin-only share reports ``0/0``. After a client account joins as a
        member, the share reports ``0/1`` until its WebSocket connects.
        """
        store = get_auth_store()

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as admin_client:
            resp = await admin_client.post("/api/v1/buckets", params={"bucket": "presbucket"})
            assert resp.status_code == 201, resp.text

            try:
                # Admin-only share: no client members.
                resp = await admin_client.get("/api/v1/buckets/presbucket/presence")
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"connected": 0, "total": 0}

                # A non-admin client joins the share.
                member_id, member_token = store.create_account("presmember", "client")
                member = store.get_account(member_id)
                assert member is not None
                store.grant_member("presbucket", member, "admin-caller", caller_is_admin=True)

                resp = await admin_client.get("/api/v1/buckets/presbucket/presence")
                assert resp.json() == {"connected": 0, "total": 1}

                # The member's own view of the share matches (self included).
                async with AsyncClient(
                    transport=ASGITransport(app=test_app),
                    base_url="http://test",
                    headers={"Authorization": f"Bearer {member_token}"},
                ) as member_client:
                    resp = await member_client.get("/api/v1/buckets/presbucket/presence")
                    assert resp.json() == {"connected": 0, "total": 1}
            finally:
                await admin_client.delete("/api/v1/buckets/presbucket")

    @pytest.mark.asyncio
    async def test_presence_requires_access(self, client):
        """A nonexistent bucket is indistinguishable from an inaccessible one."""
        resp = await client.get("/api/v1/buckets/nope/presence")
        assert resp.status_code == 404
