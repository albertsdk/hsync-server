# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the WebSocket event-relay use case.

Drives the real server over loopback WebSocket only. Verifies the handshake,
fan-out / echo-suppression, cid eviction, and rejection of bad auth, protocol
mismatch, unsafe paths and bad URL segments — without touching internals.
"""

import asyncio
import json
import os

import httpx
import pytest
import websockets
from hsync_common.protocol.handshake import PROTOCOL_VERSION_WS, build_ws_auth_request
from websockets.exceptions import ConnectionClosed

from tests.ws_helpers import (
    connect_ws_auth as _connect_auth,
)
from tests.ws_helpers import (
    make_cid as _cid,
)
from tests.ws_helpers import (
    open_ws as _open,
)
from tests.ws_helpers import (
    recv_event as _recv_event,
)
from tests.ws_helpers import (
    ws_base as _ws_base,
)

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]


class TestWsHandshake:
    async def test_successful_handshake_returns_capabilities(
        self, live_server: tuple[str, str], bb_bucket: str
    ):
        # Given a valid bucket and API key
        ws_base, api_key = _ws_base(live_server), live_server[1]

        # When authenticating over WebSocket
        ws, reply = await _connect_auth(ws_base, bb_bucket, _cid("subA"), api_key)

        # Then the server replies with an auth-ok frame advertising protocol + caps
        try:
            assert reply["type"] == "auth"
            assert reply["status"] == "ok"
            assert reply["protocol_version"] == PROTOCOL_VERSION_WS
            assert "capabilities" in reply
        finally:
            await ws.close()


class TestWsRelay:
    async def test_upload_relayed_to_other_subscriber_no_self_echo(
        self, live_server: tuple[str, str], bb_bucket: str, bb_upload
    ):
        # Given two authenticated subscribers and an HTTP client
        ws_base, api_key = _ws_base(live_server), live_server[1]
        bb_http = httpx.Client(
            base_url=live_server[0],
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        cid_a, cid_b = _cid("subA"), _cid("subB")
        sub_a, _ = await _connect_auth(ws_base, bb_bucket, cid_a, api_key)
        sub_b, _ = await _connect_auth(ws_base, bb_bucket, cid_b, api_key)
        try:
            # When subscriber A uploads a file (REST cid=cid_a matches A's WS cid)
            bb_upload(bb_http, bb_bucket, "relay.bin", b"relayed", cid=cid_a)

            # Then subscriber B receives the added event originated by A
            msg = await _recv_event(sub_b)
            assert msg["name"] == "added"
            assert msg["cid"] == cid_a
            assert msg["src"]["name"] == "relay.bin"

            # And subscriber A does NOT receive its own event (echo suppression)
            with pytest.raises(asyncio.TimeoutError):
                await _recv_event(sub_a, timeout=1.5)
        finally:
            await sub_a.close()
            await sub_b.close()
            bb_http.close()

    async def test_reconnecting_cid_evicts_stale_registration(
        self, live_server: tuple[str, str], bb_bucket: str
    ):
        # Given an already-connected cid
        ws_base, api_key = _ws_base(live_server), live_server[1]
        first = None
        second = None
        try:
            first, _ = await _connect_auth(ws_base, bb_bucket, _cid("dup"), api_key)
            # When a second connection reuses the same cid
            second, _ = await _connect_auth(ws_base, bb_bucket, _cid("dup"), api_key)
            # Then the first connection is closed by the server (presence
            # frames may arrive before the closure).
            with pytest.raises(ConnectionClosed):
                while True:
                    msg = json.loads(await asyncio.wait_for(first.recv(), timeout=5))
                    assert msg.get("type") == "presence"
        finally:
            if second is not None:
                await second.close()
            if first is not None:
                await first.close()

    async def test_fan_out_survives_one_client_disconnect(
        self, live_server: tuple[str, str], bb_bucket: str, bb_upload
    ):
        """When one subscriber disconnects during broadcast, the others still
        receive the event — the faulty connection must never abort delivery."""
        ws_base, api_key = _ws_base(live_server), live_server[1]
        bb_http = httpx.Client(
            base_url=live_server[0],
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        cid_a = _cid("fanA")
        sub_a = sub_b = sub_c = None
        try:
            # Three subscribers: A (uploader), B (stays), C (leaves early)
            sub_a, _ = await _connect_auth(ws_base, bb_bucket, cid_a, api_key)
            sub_b, _ = await _connect_auth(ws_base, bb_bucket, _cid("fanB"), api_key)
            sub_c, _ = await _connect_auth(ws_base, bb_bucket, _cid("fanC"), api_key)

            # Subscriber C disconnects cleanly before the event is sent
            await sub_c.close()

            # When subscriber A uploads a file
            bb_upload(bb_http, bb_bucket, "fanout.bin", b"fan-out", cid=cid_a)

            # Then subscriber B still receives the broadcast
            msg = await _recv_event(sub_b)
            assert msg["name"] == "added"
            assert msg["cid"] == cid_a
            assert msg["src"]["name"] == "fanout.bin"

            # And subscriber A is not echoed
            with pytest.raises(asyncio.TimeoutError):
                await _recv_event(sub_a, timeout=1.5)
        finally:
            for ws in (sub_a, sub_b):
                if ws is not None:
                    await ws.close()
            bb_http.close()


class TestWsNegative:
    async def test_bad_token_closes_connection(self, live_server: tuple[str, str], bb_bucket: str):
        # Given a wrong API key (and a *valid* cid, so the rejection must come
        # from the token gate, not the cid gate)
        ws_base, _ = _ws_base(live_server), live_server[1]

        # When authenticating with it
        ws = await _open(ws_base, bb_bucket, _cid("badtoken"), "wrong-key")

        # Then the server closes the connection with 4003 Invalid API key
        # (4000 would mean the cid was rejected instead — a different gate)
        with pytest.raises(ConnectionClosed) as exc_info:
            await asyncio.wait_for(ws.recv(), timeout=5)
        # ``rcvd`` is the close frame the server sent (``ConnectionClosed.code``
        # is deprecated since websockets 13.1).
        closed = exc_info.value
        assert closed.rcvd is not None
        assert closed.rcvd.code == 4003
        await ws.close()

    async def test_protocol_mismatch_rejected(self, live_server: tuple[str, str], bb_bucket: str):
        # Given an incompatible protocol version
        ws_base, api_key = _ws_base(live_server), live_server[1]

        # When authenticating with protocol_version=ws/999
        ws, reply = await _connect_auth(
            ws_base, bb_bucket, _cid("proto"), api_key, protocol_version="ws/999"
        )

        # Then the server replies with an auth error and closes the connection
        try:
            assert reply["status"] == "error"
            assert reply["error"] == "protocol_incompatible"
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=5)
        finally:
            await ws.close()

    async def test_traversal_event_path_rejected(
        self, live_server: tuple[str, str], bb_bucket: str, bb_http: httpx.Client
    ):
        # Given a sibling bucket (empty) and an authenticated client in another bucket
        ws_base, api_key = _ws_base(live_server), live_server[1]
        sibling = "bb-sibling-keep"
        assert bb_http.post("/api/v1/buckets", params={"bucket": sibling}).status_code == 201
        evil_cid = _cid("evil")
        ws, _ = await _connect_auth(ws_base, bb_bucket, evil_cid, api_key)
        try:
            # When the client sends an "added" event with a traversal src path
            await ws.send(
                json.dumps(
                    {
                        "name": "added",
                        "cid": evil_cid,
                        "bucket": bb_bucket,
                        "src": {
                            "filepath": f"../{sibling}/evil.txt",
                            "name": "evil.txt",
                            "size": 0,
                            "sha256sum": "",
                            "ctime": 0,
                            "mtime": 0,
                            "permissions": "644",
                        },
                        "dst": None,
                    }
                )
            )
            await asyncio.sleep(0.5)

            # Then no file was created in the sibling bucket (traversal rejected)
            listed = bb_http.get(f"/api/v1/buckets/{sibling}/files").json()
            assert all(e["name"] != "evil.txt" for e in listed)
        finally:
            await ws.close()
            bb_http.delete(f"/api/v1/buckets/{sibling}")

    async def test_url_encoded_dotdot_cid_rejected(self, live_server: tuple[str, str]):
        # Given a cid segment that would escape the storage root when decoded
        ws_base, api_key = _ws_base(live_server), live_server[1]
        uri = f"{ws_base}/api/v1/buckets/anybucket/events/%2e%2e"

        # When connecting with that cid and sending an auth frame
        ws = await websockets.connect(uri, max_size=None)
        try:
            try:
                await ws.send(
                    json.dumps(
                        build_ws_auth_request(token=api_key, app_version="bb-test", capabilities=[])
                    )
                )
            except ConnectionClosed:
                pass

            # Then the server rejects the invalid cid (connection closed)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=5)
        finally:
            await ws.close()
