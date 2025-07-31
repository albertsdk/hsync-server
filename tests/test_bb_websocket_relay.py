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

import httpx
import pytest
import websockets
from hsync_common.protocol.handshake import PROTOCOL_VERSION_WS, build_ws_auth_request
from websockets.exceptions import ConnectionClosed

pytestmark = pytest.mark.usefixtures("bb_http")


def _ws_base(live_server: tuple[str, str]) -> str:
    base, _ = live_server
    return base.replace("http://", "ws://")


async def _open(
    ws_base: str,
    bucket: str,
    cid: str,
    api_key: str,
    *,
    protocol_version: str = PROTOCOL_VERSION_WS,
    token: str | None = None,
):
    """Open a WS to the events endpoint and send the auth frame (no recv).

    A close during/before send is tolerated: callers expecting rejection read
    with ``recv()`` and assert a :class:`ConnectionClosed`.
    """
    uri = f"{ws_base}/api/v1/buckets/{bucket}/events/{cid}"
    ws = await websockets.connect(uri, max_size=None)
    frame = build_ws_auth_request(
        token=token if token is not None else api_key,
        app_version="bb-test",
        capabilities=[],
    )
    frame["protocol_version"] = protocol_version
    try:
        await ws.send(json.dumps(frame))
    except ConnectionClosed:
        pass
    return ws


async def _connect_auth(
    ws_base: str,
    bucket: str,
    cid: str,
    api_key: str,
    *,
    protocol_version: str = PROTOCOL_VERSION_WS,
    token: str | None = None,
):
    """Open a WS, send auth, return (websocket, auth_reply_dict)."""
    ws = await _open(ws_base, bucket, cid, api_key, protocol_version=protocol_version, token=token)
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    return ws, reply


class TestWsHandshake:
    async def test_successful_handshake_returns_capabilities(
        self, live_server: tuple[str, str], bb_bucket: str
    ):
        # Given a valid bucket and API key
        ws_base, api_key = _ws_base(live_server), live_server[1]

        # When authenticating over WebSocket
        ws, reply = await _connect_auth(ws_base, bb_bucket, "subA", api_key)

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
        sub_a, _ = await _connect_auth(ws_base, bb_bucket, "subA", api_key)
        sub_b, _ = await _connect_auth(ws_base, bb_bucket, "subB", api_key)
        try:
            # When subscriber A uploads a file (REST cid=subA matches A's WS cid)
            bb_upload(bb_http, bb_bucket, "relay.bin", b"relayed", cid="subA")

            # Then subscriber B receives the added event originated by A
            msg = json.loads(await asyncio.wait_for(sub_b.recv(), timeout=5))
            assert msg["name"] == "added"
            assert msg["cid"] == "subA"
            assert msg["src"]["name"] == "relay.bin"

            # And subscriber A does NOT receive its own event (echo suppression)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sub_a.recv(), timeout=1.5)
        finally:
            await sub_a.close()
            await sub_b.close()
            bb_http.close()

    async def test_reconnecting_cid_evicts_stale_registration(
        self, live_server: tuple[str, str], bb_bucket: str
    ):
        # Given an already-connected cid
        ws_base, api_key = _ws_base(live_server), live_server[1]
        first, _ = await _connect_auth(ws_base, bb_bucket, "dup", api_key)
        try:
            # When a second connection reuses the same cid
            second, _ = await _connect_auth(ws_base, bb_bucket, "dup", api_key)
            try:
                # Then the first connection is closed by the server
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(first.recv(), timeout=5)
            finally:
                await second.close()
        except Exception:
            await first.close()
            raise

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
        try:
            # Three subscribers: A (uploader), B (stays), C (leaves early)
            sub_a, _ = await _connect_auth(ws_base, bb_bucket, "fanA", api_key)
            sub_b, _ = await _connect_auth(ws_base, bb_bucket, "fanB", api_key)
            sub_c, _ = await _connect_auth(ws_base, bb_bucket, "fanC", api_key)

            # Subscriber C disconnects cleanly before the event is sent
            await sub_c.close()

            # When subscriber A uploads a file
            bb_upload(bb_http, bb_bucket, "fanout.bin", b"fan-out", cid="fanA")

            # Then subscriber B still receives the broadcast
            msg = json.loads(await asyncio.wait_for(sub_b.recv(), timeout=5))
            assert msg["name"] == "added"
            assert msg["cid"] == "fanA"
            assert msg["src"]["name"] == "fanout.bin"

            # And subscriber A is not echoed
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sub_a.recv(), timeout=1.5)
        finally:
            for ws in (sub_a, sub_b):
                await ws.close()
            bb_http.close()


class TestWsNegative:
    async def test_bad_token_closes_connection(self, live_server: tuple[str, str], bb_bucket: str):
        # Given a wrong API key
        ws_base, _ = _ws_base(live_server), live_server[1]

        # When authenticating with it
        ws = await _open(ws_base, bb_bucket, "bad", "wrong-key")

        # Then the server closes the connection (no auth reply)
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5)
        await ws.close()

    async def test_protocol_mismatch_rejected(self, live_server: tuple[str, str], bb_bucket: str):
        # Given an incompatible protocol version
        ws_base, api_key = _ws_base(live_server), live_server[1]

        # When authenticating with protocol_version=ws/999
        ws, reply = await _connect_auth(
            ws_base, bb_bucket, "proto", api_key, protocol_version="ws/999"
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
        ws, _ = await _connect_auth(ws_base, bb_bucket, "evil", api_key)
        try:
            # When the client sends an "added" event with a traversal src path
            await ws.send(
                json.dumps(
                    {
                        "name": "added",
                        "cid": "evil",
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
