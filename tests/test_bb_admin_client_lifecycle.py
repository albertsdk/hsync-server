# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the admin account lifecycle.

Drives the real server over HTTP only: create a client, prove its token works
(non-admin → 403 on an admin route), rotate it, disable/enable it and clear its
expiry. A token's validity is observed as ``403`` (authenticated, wrong role),
``401`` (rejected/unknown token) or ``in (401, 403)`` when an account is
disabled and the layer that rejects it is not part of the contract.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os
import time

import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)

_ADMIN_ROUTE = "/api/v1/admin/clients"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAdminClientLifecycle:
    def test_create_rotate_disable_enable_and_clear_expiry(self, bb_http, bb_sample):
        # Given a freshly created client account (and its one-time token)
        name = bb_sample(prefix="acct").name
        created = bb_http.post(_ADMIN_ROUTE, json={"name": name, "role": "client"})
        assert created.status_code == 201, created.text
        account = created.json()
        client_id, token = account["id"], account["token"]

        # Then the token authenticates (403 = authenticated, not admin)…
        assert bb_http.get(_ADMIN_ROUTE, headers=_bearer(token)).status_code == 403
        # …the listing includes the account and never the token/hash.
        listed = bb_http.get(_ADMIN_ROUTE).json()["clients"]
        entry = next(c for c in listed if c["id"] == client_id)
        assert "token" not in entry and "hash" not in " ".join(entry).lower()

        # When the key is rotated with the minimum one-second grace window
        rotated = bb_http.post(f"{_ADMIN_ROUTE}/{client_id}/rotate-key", json={"grace_seconds": 1})
        assert rotated.status_code == 200, rotated.text
        new_token = rotated.json()["token"]

        # Then after the grace window the old token is rejected and the new one works
        time.sleep(1.3)
        assert bb_http.get(_ADMIN_ROUTE, headers=_bearer(token)).status_code == 401
        assert bb_http.get(_ADMIN_ROUTE, headers=_bearer(new_token)).status_code == 403

        # When the account is disabled
        assert bb_http.post(f"{_ADMIN_ROUTE}/{client_id}/disable").status_code == 200
        # Then its token stops authenticating
        assert bb_http.get(_ADMIN_ROUTE, headers=_bearer(new_token)).status_code in (401, 403)

        # When it is enabled again, the token authenticates once more
        assert bb_http.post(f"{_ADMIN_ROUTE}/{client_id}/enable").status_code == 200
        assert bb_http.get(_ADMIN_ROUTE, headers=_bearer(new_token)).status_code == 403

        # When the expiry is cleared, the account reports no key expiry
        cleared = bb_http.patch(f"{_ADMIN_ROUTE}/{client_id}/expiry", json={"none": True})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["key_expires_at"] is None

        # Cleanup
        assert bb_http.delete(f"{_ADMIN_ROUTE}/{client_id}").status_code == 200

    def test_disable_own_account_is_refused(self, bb_http):
        """An admin cannot disable the account it is authenticated with."""
        me = bb_http.get(_ADMIN_ROUTE).json()["clients"]
        # The built-in admin is the only admin seeded by the live server.
        admin = next(c for c in me if c["role"] == "admin")
        r = bb_http.post(f"{_ADMIN_ROUTE}/{admin['id']}/disable")
        assert r.status_code == 409, r.text
