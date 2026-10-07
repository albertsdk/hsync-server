# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared helpers for driving the server's black-box WebSocket surface.

Used by every black-box module that needs an authenticated WS session
(relay, concurrency stress). The helpers speak the public wire protocol only:
connect, send the documented auth frame, and receive frames while skipping
``presence``/``ping``.
"""

import asyncio
import json

import websockets
from hsync_common.protocol.handshake import PROTOCOL_VERSION_WS, build_ws_auth_request
from websockets.exceptions import ConnectionClosed


def ws_base(live_server: tuple[str, str]) -> str:
    """Return the ``ws://`` base URL of the module-scoped live server."""
    base, _ = live_server
    return base.replace("http://", "ws://")


def make_cid(tag: str) -> str:
    """Build a wire-valid cid (exactly 21 alphanumeric chars) from a short tag.

    The server rejects any other shape at accept time (``is_valid_cid``), so
    short readable tags like "subA" would be refused with 4000 before any
    token/protocol check ever runs.
    """
    base = f"bb{tag}"
    return base + "0" * (21 - len(base))


async def open_ws(
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


async def connect_ws_auth(
    ws_base: str,
    bucket: str,
    cid: str,
    api_key: str,
    *,
    protocol_version: str = PROTOCOL_VERSION_WS,
    token: str | None = None,
):
    """Open a WS, send auth, return (websocket, auth_reply_dict)."""
    ws = await open_ws(
        ws_base, bucket, cid, api_key, protocol_version=protocol_version, token=token
    )
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    return ws, reply


async def recv_event(ws, timeout: float = 5.0) -> dict:
    """Receive the next file event, skipping ``presence``/``ping`` frames.

    Presence frames arrive whenever another client joins/leaves the bucket;
    heartbeat ``ping`` frames arrive every ``HSYNC_WS_HEARTBEAT_INTERVAL``.
    Black-box event assertions are only interested in file events.
    """
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") not in ("presence", "ping"):
            return msg
