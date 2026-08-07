# Authentication — Server Mode (Server-Side)

This document describes how the **HSYNC Server** authenticates incoming WebSocket
connections from clients in **Server Mode**. For the shared contracts (CID format,
message schema, protocol versioning), see
[`hsync-common/docs/authentication.md`](https://github.com/albertobasso/hsync-common/blob/main/docs/authentication.md).

## Overview

In Server Mode, the HSYNC Server is the central hub. Every client connects to it
over WSS and presents an **API key** as a bearer token. The server validates the
token, verifies protocol compatibility, and registers the client's CID for event
attribution and echo suppression.

## Authentication flow

```
Client                              HSYNC Server
  │                                       │
  │── WSS connect ───────────────────────▶│
  │     /api/v1/events/{cid}              │
  │                                       │
  │── {"type": "auth",                    │
  │    "token": "<HSYNC_API_KEY>",        │
  │    "app_version": "1.2.3",            │
  │    "protocol_version": "ws/2",        │
  │    "capabilities": ["compression",    │
  │                     "delta"]} ───────▶│
  │                                       │── validate protocol_version
  │                                       │   ├── ❌ incompatible → close
  │                                       │   │    (reason: protocol_mismatch)
  │                                       │   └── ✅ compatible → continue
  │                                       │── validate CID format (is_valid_cid)
  │                                       │   ├── ❌ malformed → close
  │                                       │   └── ✅ → continue
  │                                       │── validate API key
  │                                       │   ├── ❌ wrong/expired → close
  │                                       │   └── ✅ → register CID + connection
  │                                       │
  │◀── {"type": "auth",                   │
  │     "status": "ok",                   │
  │     "app_version": "1.2.3",           │
  │     "protocol_version": "ws/2",       │
  │     "capabilities": ["compression",   │
  │                      "delta"]} ───────│
  │                                       │
  │◀── {"name": "added", ...} ────────────│  (file change events)
  │◀── {"name": "modified", ...} ─────────│
  │◀── {"name": "deleted", ...} ──────────│
```

## Key points

1. **WSS only**: there is no plain-WS fallback. The transport is encrypted end-to-end.
2. **CID in the URL path**: the client's CID is part of the WebSocket URL (`/api/v1/events/{cid}`). The server extracts it from the path and validates it with `is_valid_cid()` before accepting the connection.
3. **API key as bearer token**: the `token` field carries the server's API key (`HSYNC_API_KEY`). No key → no connection.
4. **Protocol version enforcement**: a `protocol_version` mismatch between client and server is a hard disconnect. There is no fallback to an older protocol.
5. **Capability negotiation**: the server advertises its capabilities (`compression`, `delta`) in the auth reply. The client must respect the server's advertised capabilities.
6. **One CID per connection**: the CID is registered for the lifetime of the WebSocket. If a second connection arrives with the same CID, the older connection is evicted (stale connection replacement).

## Source code

| Component | File | Lines |
|---|---|---|
| CID validation on WS connect | `hsync_server/api/events.py` | `is_valid_cid(cid)` before accepting |
| Auth frame handling | `hsync_server/api/events.py` | validate token, protocol version, capabilities |
| Connection manager | `hsync_server/api/connection_manager.py` | register/evict CID, track active connections |
| Protocol constants | `hsync_common/protocol/handshake.py` | `PROTOCOL_VERSION_REST`, `PROTOCOL_VERSION_WS` |
