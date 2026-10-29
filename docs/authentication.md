# Authentication — Server Mode (Server-Side)

This document describes how the **HSYNC Server** authenticates clients in
**Server Mode** using per-account tokens. For the shared contracts (CID format,
message schema, protocol versioning), see
[`hsync-common/docs/authentication.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/authentication.md).

## Overview

The server authenticates each client with a **per-account token** of the form
`hs_<account_id>_<secret>`. Each account has a role (`admin` or `client`) and
bucket memberships. The admin role can manage accounts and access all buckets;
a client role accesses only buckets it owns or is a member of.

## Token format

```text
hs_<account_id 21 alnum>_<secret 64 hex>
```

The token is presented as `Authorization: Bearer <token>` (REST) and as the
`token` field of the WS `auth` frame. Clients treat the token as an opaque
string — no parsing is required on the client side.

## Rendezvous

On first start (empty account store), an admin account is created:

- If `HSYNC_ADMIN_KEY` (64 lowercase hex) is set in the environment: used
  silently as the admin secret. Ideal for CI/deploy automation.
- If absent: a secret is generated and the full token `hs_<id>_<secret>` is
  delivered once. On an interactive terminal it is printed to stderr; when
  stderr is not a TTY (systemd/journald, container log drivers) it is written to
  `<state_dir>/rendezvous_admin_token` with mode `0600`, so it never reaches a
  captured log stream. **It is not recoverable**: move it to your secret store
  and delete the file.

The admin account has `name=admin`, `id=HsyncAdminRendezvous01`, `role=admin`.

## Authentication flow (WebSocket)

```text
Client                              HSYNC Server
  │                                       │
  │── WSS connect ───────────────────────▶│
  │     /api/v1/buckets/{bucket}/events/{cid} │
  │                                       │
  │── {"type": "auth",                    │
  │    "token": "hs_<id>_<secret>",       │
  │    "app_version": "1.2.3",            │
  │    "protocol_version": "ws/1",        │
  │    "capabilities": ["compression",    │
  │                     "delta"]} ───────▶│
  │                                       │── validate protocol_version
  │                                       │── validate CID format
  │                                       │── verify account token (scrypt)
  │                                       │── check bucket access (membership)
  │                                       │   ├── ❌ no access → close 4009
  │                                       │   └── ✅ → register connection
  │                                       │
  │◀── {"type": "auth", "status": "ok"} ──│
  │                                       │
  │◀── {"name": "added", ...} ────────────│  (file change events)
```

## Authorization

| Action | Admin | Owner | Member | Others |
|---|---|---|---|---|
| Read/sync bucket data | ✅ all | ✅ own | ✅ member | 404 / WS 4009 |
| Create bucket | ✅ | ✅ (becomes owner) | ✅ (becomes owner) | ✅ |
| Delete bucket | ✅ | 403 | 403 | 404 |
| Grant/revoke membership | ✅ any | ✅ own | 403 | 404 |
| Transfer ownership | ✅ any | ✅ own | 403 | 404 |
| Manage accounts | ✅ | 403 | 403 | 403 |

Non-members and non-existent buckets return the same 404 (REST) or 4009 (WS) —
no existence leak.

## Key points

1. **WSS only**: the transport is encrypted end-to-end.
2. **CID in the URL path**: the session CID is part of the WebSocket URL. The
   account id is inside the token — they are distinct identities.
3. **Account token as bearer**: the `token` field carries the account token.
   Invalid, disabled, or expired tokens close with 4003.
4. **Bucket access check**: after auth, the server checks that the account has
   access to the requested bucket. Failure closes with 4009 (indistinguishable
   from "bucket not found").
5. **Revocation**: rotate-key (immediate) and disable close WS connections on
   the local worker; other workers re-check on every frame, ping, and broadcast.
6. **CLI recovery**: `hsync-server admin client rotate-key --name admin` from
   the host filesystem recovers access without a token.

## Source code

| Component | File |
|---|---|
| Account store (tables, scrypt, rendezvous, CRUD) | `hsync_server/auth_store/` |
| Token verification + AuthContext + dependencies | `hsync_server/auth.py` |
| Bucket endpoints (ownership, authz) | `hsync_server/api/buckets.py` |
| Membership/owner API | `hsync_server/api/members.py` |
| Admin REST API | `hsync_server/api/admin.py` |
| WebSocket auth + re-checks | `hsync_server/api/events.py` |
| Connection manager (account binding) | `hsync_server/api/connection_manager.py` |
| CLI admin commands | `hsync_server/__main__.py` |
