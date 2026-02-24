# HSYNC Server — Roles, Permissions and Bucket Management

This guide describes the HSYNC Server access-control model: the three user
roles (`admin`, `owner`, `member`), their permissions and scope, bucket
property management, authentication-key management, and the available tools
(CLI and REST API) with concrete examples.

> **Terminology.** In HSYNC an "account" is an authenticated identity holding a
> token `hs_<id>_<secret>`. A "client" is a device that uses an account. In this
> guide, "user" is a synonym for account.

---

## Table of Contents

- [Role Model](#role-model)
- [Permission Matrix](#permission-matrix)
- [Role Details](#role-details)
- [Hierarchy and Integrity Rules](#hierarchy-and-integrity-rules)
- [Bucket Property Management](#bucket-property-management)
- [Authentication-Key Management](#authentication-key-management)
- [CLI Tools](#cli-tools)
- [REST API](#rest-api)
- [Status Codes and WebSocket Close Codes](#status-codes-and-websocket-close-codes)
- [Security Guarantees](#security-guarantees)

---

## Role Model

HSYNC defines **three roles** across two scopes:

| Role | Scope | Nature |
|---|---|---|
| `admin` | **Global** (the whole installation) | Server administrator |
| `owner` | **Per-bucket** | Owner of a specific bucket |
| `member` | **Per-bucket** | Member with data access to a bucket |

An account has **exactly one role** (`admin` or `client`), immutable after
creation. The relationship with buckets is expressed through **membership**: a
`client` account can be the `owner` of some buckets and a `member` of others, or
have no relationship with a bucket at all.

```
Account (role: admin | client)
   │
   ├── admin  → access to ALL buckets + account management
   │
   └── client → per-bucket membership:
                  ├── owner  (exactly one per bucket)
                  └── member (zero or more per bucket)
```

### Hierarchy

```
admin
  ├── manages all accounts (including other admins)
  ├── operates on all buckets
  └── can do everything an owner/member can do

owner (of a bucket)
  ├── manages membership of their own bucket (grant/revoke member)
  ├── transfers ownership
  └── same data access as a member

member (of a bucket)
  └── data access (read/write/sync) to the bucket
```

- **An admin is not automatically an owner** of buckets: the admin role grants
  access to all buckets, but ownership (relevant for cascade delete and
  transfer) still belongs to a specific owner.
- **Owner and member have identical data access.** The difference is purely
  administrative: only the owner (besides the admin) can modify membership and
  transfer ownership.

---

## Permission Matrix

| Action | admin | owner | member | Others / nonexistent |
|---|---|---|---|---|
| Read/write/sync bucket data (REST + WS) | ✅ | ✅ | ✅ | 404 / WS 4009 |
| Create a bucket (`POST /buckets`) | ✅ (becomes owner) | — | — | ✅ (becomes owner); name taken → 409 |
| Delete a bucket (`DELETE /buckets/{b}`) | ✅ | ❌ 403 | ❌ 403 | 404 |
| List members (`GET .../members`) | ✅ 200 | ✅ 200 | ❌ 403 | 404 |
| Grant/revoke membership | ✅ (any bucket) | ✅ (own bucket) | ❌ 403 | 404 |
| Transfer ownership / reclaim orphan | ✅ (any bucket) | ✅ (own bucket) | ❌ 403 | 404 |
| `/admin/*` endpoints (accounts, inventory) | ✅ | ❌ 403 | ❌ 403 | 403 (authenticated non-admin) / 401 (unauthenticated) |

### Anti-enumeration

To avoid leaking the existence of buckets an account cannot access:

- A **non-member** and a **nonexistent bucket** produce the **same** response:
  `404` (REST) or close code `4009` (WebSocket).
- A **member** attempting an administrative operation (grant/transfer/delete)
  receives `403` (they already know the bucket exists, so there is nothing to
  hide).
- An **invalid/disabled/expired token** always yields `401` (REST) or `4003`
  (WS), indistinguishable from a nonexistent account.

---

## Role Details

### Administrator (`admin`)

**Allowed operations:**
- Create, list, disable, enable and delete accounts.
- Rotate keys and set expiry for any account.
- Create additional `admin` accounts.
- Read, write and sync **all** buckets.
- Delete any bucket (filesystem + DB cleanup).
- Grant/revoke membership and transfer ownership on **any** bucket.
- Reclaim orphan buckets (on-disk directories without an owner).
- View the global inventory (`GET /admin/buckets`), including orphans and
  "ghost" entries.

**Forbidden operations / constraints:**
- Cannot disable or delete the **last active admin** via API (409). Only the
  local CLI can force it (`--force`).
- Cannot disable **themselves** via API (409) — another admin is required.
- Cannot rename an account or change the role of an existing account (role is
  immutable; recreate the account instead).

### Owner (`owner`)

**Allowed operations (on their own bucket):**
- Read, write and sync data.
- List members.
- Grant membership (`role=member`) to other active accounts.
- Revoke a member's membership.
- Transfer ownership to another active account (the ex-owner stays `member`).

**Forbidden operations:**
- **Delete the bucket** (admin only).
- Grant membership to themselves or to an account that is already owner/member
  (409).
- Grant membership or transfer to a `disabled` account (409).
- Revoke the last owner without transferring first (409).
- Reclaim orphan buckets (admin only).

### Member (`member`)

**Allowed operations:**
- Read, write and sync bucket data.

**Forbidden operations:**
- List members (403).
- Grant/revoke membership (403).
- Transfer ownership (403).
- Delete the bucket (403).

---

## Hierarchy and Integrity Rules

- **Exactly one owner per bucket** (partial `UNIQUE` index on `bucket_members`
  with `role='owner'`).
- **An account can own multiple buckets and be a member of multiple buckets.**
- **Ownership transfer:** the recipient is promoted to owner even if they were
  not a member; the ex-owner is demoted to `member`. Atomic, in a single
  transaction.
- **Cascade account delete:** deleting an account with `cascade=true` also
  deletes the buckets it owns; its memberships as a member are removed. Without
  `cascade`, deletion is refused (409) if the account owns buckets.
- **Last active admin:** the API refuses (409) any operation that would leave
  zero active admins. The count is checked inside `BEGIN IMMEDIATE` to avoid
  races between workers.
- **Disabled account:** membership is preserved; access returns on re-enable. A
  disabled owner cannot manage the bucket until re-enabled or until an admin
  transfers ownership.

---

## Bucket Property Management

HSYNC does **not** expose per-bucket configurable properties (unlike S3/MinIO).
The properties below are **global server settings**, applied uniformly to all
buckets.

### Encryption

| Aspect | Behaviour |
|---|---|
| In transit | TLS/WSS required in production (a reverse proxy terminates TLS). Clients refuse non-encrypted connections to remote servers. |
| At rest | **Operator responsibility** (e.g. LUKS/disk encryption). There is no per-bucket encryption option. |
| Secrets at rest | Account keys are stored as PHC-style `scrypt` hashes (N=2^17, r=8, p=1). The plaintext secret exists only at emission time. |

### Versioning

HSYNC uses **causal versioning** (vector clocks) for sync conflict resolution —
**not** S3-style object versioning (there is no "list versions of a file"). The
relevant components are:

- **Causal versions** (`versions` table): track the state of each file to
  arbitrate conflicts between clients.
- **Tombstones** (deletion markers): record deletions so they propagate
  correctly to other clients. Subject to a TTL (see Lifecycle).

### Access Policy

The access policy **is** the membership model described in the Role Model and
Permission Matrix sections. There are no IAM-style policy documents:
authorization is determined by `(account role, bucket membership)`.

### Logging

- **Structured logging** via `loguru`, with per-action labels (see
  `docs/log-labels.md`).
- **Audit without secrets:** logs show `key_prefix` (first 8 hex chars of the
  secret) plus account id/name — **never** the full token.
- **`last_used_at`** tracks each account's last use (throttled to 1 update /
  5 minutes).
- The bootstrap token is printed **once** to stderr, never to application logs.

### Lifecycle

| Setting | Default | Description |
|---|---|---|
| `HSYNC_TOMBSTONE_TTL_DAYS` | configured (0 = forever) | Days to retain deletion markers before garbage collection. |
| `HSYNC_DELTA_TOKEN_TTL` | 600 s (min 30) | Validity of delta download tokens. |
| `key_expires_at` (per account) | no expiry | Optional expiry of an account's key. |

### Other global properties

| Setting | Default | Description |
|---|---|---|
| `HSYNC_COMPRESSION_ENABLED` | `true` | zstd compression of transfers. |
| `HSYNC_DELTA_ENABLED` | `true` | rsync-style delta transfer. |
| `HSYNC_DELTA_MIN_FILE_SIZE` | 256 KB | Minimum size to use delta transfer. |
| `HSYNC_MAX_UPLOAD_SIZE` | configured | Upload size limit. |
| `HSYNC_CORS_ENABLED` / `HSYNC_CORS_ORIGINS` | `false` / — | CORS control. |
| Rate limits (`HSYNC_RATE_LIMIT_*`) | various | Per-IP limits; admin endpoints use a stricter limit (`20/minute`). |

---

## Authentication-Key Management

### Token format

```
hs_<account_id 21 alnum>_<secret 64 hex>
```

- `hs_` — opaque prefix (clients do not interpret it).
- `account_id` — 21 alphanumeric characters, server-minted, immutable.
- `secret` — 64 lowercase hex characters (`secrets.token_hex(32)`).

The token is presented as `Authorization: Bearer <token>` (REST) and in the
`token` field of the WS `auth` frame. Clients treat it as an opaque string.

### Key lifecycle

| Operation | Description |
|---|---|
| **Creation** | The token is returned **once** (at account creation or rotation). It is not recoverable. |
| **Rotation** | Generates a new secret and increments `key_generation`. The old key is invalidated immediately (WS connections are closed). |
| **Grace period** | With `grace_seconds`, the old key stays valid for a limited window (useful for client migration). |
| **Expiry** | `expires_at` (RFC3339) or `valid_days`; `none` removes the expiry. |
| **Revocation** | `disable` blocks authentication (and closes WS) without deleting the account. `enable` restores it. |
| **Deletion** | `delete` removes the account (with `cascade` it also deletes owned buckets). |

### Key scope

Keys do **not** have granular scopes (there are no keys limited to a single
bucket or a subset of operations). Scope is determined by the **account role**:

- An `admin` account's key → full access (all buckets + account management).
- A `client` account's key → access only to buckets it owns or is a member of.

To restrict an account to a subset of buckets, use **membership** (grant access
only to the desired buckets).

### Admin bootstrap

On first start (empty store), the bootstrap admin account is created
(`name=admin`, `id=HsyncAdminBootstrap01`, `role=admin`):

- If `HSYNC_ADMIN_KEY` (64 hex) is set → used as the secret (silent).
- If absent → the server generates a secret and prints the token **once**.
- If set after bootstrap → ignored (warning).

---

## CLI Tools

The CLI operates **directly on the store's filesystem** (filesystem authority),
so it does **not** require a token. It is the local administration and recovery
tool. All commands live under `hsync-server admin`.

### Account management (`admin client`)

```bash
# Create an account (the token is printed once)
hsync-server admin client add --name phone --role client
hsync-server admin client add --name ops --role admin --valid-days 90

# List accounts (id, name, role, status, generation, prefix, expiry)
hsync-server admin client list

# Rotate a key (the new token is printed once)
hsync-server admin client rotate-key --name phone
hsync-server admin client rotate-key --id <account_id> --grace 3600

# Set/extend/remove expiry
hsync-server admin client set-expiry --name phone --valid-days 30
hsync-server admin client set-expiry --name phone --expires-at 2026-12-31T00:00:00Z
hsync-server admin client set-expiry --name phone --none

# Disable / re-enable an account
hsync-server admin client disable --name phone
hsync-server admin client enable --name phone

# Delete an account (--cascade deletes owned buckets)
hsync-server admin client remove --name phone
hsync-server admin client remove --name phone --cascade

# Force deletion of the last admin (recovery)
hsync-server admin client remove --name admin --force
```

### Bucket and membership management (`admin bucket`)

```bash
# List all buckets (FS ∪ membership), including orphans and ghosts
hsync-server admin bucket list

# Grant membership (member role only)
hsync-server admin bucket grant --bucket photos --client phone

# Revoke membership
hsync-server admin bucket revoke --bucket photos --client phone

# Set/transfer ownership (also reclaims orphans)
hsync-server admin bucket set-owner --bucket photos --client laptop
```

### Recovery

```bash
# Recover access if the admin token is lost
hsync-server admin client rotate-key --name admin
```

---

## REST API

All endpoints require `Bearer` authentication. The `/admin/*` endpoints also
require the `admin` role.

### Account management (`/api/v1/admin/clients`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/clients` | List all accounts (never hashes/secrets). |
| `POST` | `/clients` | Create an account; returns the token once. |
| `POST` | `/clients/{id}/rotate-key` | Rotate the key; returns the new token. |
| `PATCH` | `/clients/{id}/expiry` | Set/extend/remove expiry. |
| `POST` | `/clients/{id}/disable` | Disable the account. |
| `POST` | `/clients/{id}/enable` | Re-enable the account. |
| `DELETE` | `/clients/{id}` | Delete the account (`?cascade=true`). |

**Examples:**

```bash
# Create a client account
curl -X POST http://localhost:9000/api/v1/admin/clients \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>" \
  -H "Content-Type: application/json" \
  -d '{"name": "phone", "role": "client", "valid_days": 90}'

# Rotate an account's key
curl -X POST http://localhost:9000/api/v1/admin/clients/<id>/rotate-key \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>" \
  -H "Content-Type: application/json" \
  -d '{"grace_seconds": 3600}'

# Disable an account
curl -X POST http://localhost:9000/api/v1/admin/clients/<id>/disable \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"

# List accounts
curl http://localhost:9000/api/v1/admin/clients \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"
```

### Bucket inventory (`/api/v1/admin/buckets`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/buckets` | List all buckets (FS ∪ membership), including orphans (`owner=null`) and ghosts (`on_disk=false`). |
| `POST` | `/buckets` | Create a bucket on behalf of a specified owner. |

**Examples:**

```bash
# Global inventory
curl http://localhost:9000/api/v1/admin/buckets \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"

# Create a bucket on behalf of an owner
curl -X POST http://localhost:9000/api/v1/admin/buckets \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>" \
  -H "Content-Type: application/json" \
  -d '{"bucket": "photos", "owner": "laptop"}'
```

### Membership and ownership (bucket path)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/buckets/{bucket}/members` | List members (owner/admin). |
| `POST` | `/api/v1/buckets/{bucket}/members` | Grant membership (owner/admin). |
| `DELETE` | `/api/v1/buckets/{bucket}/members/{client_id}` | Revoke membership (owner/admin). |
| `POST` | `/api/v1/buckets/{bucket}/owner` | Transfer ownership or reclaim orphan (owner/admin). |

**Examples:**

```bash
# Grant membership (by id or by name, exactly one)
curl -X POST http://localhost:9000/api/v1/buckets/photos/members \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>" \
  -H "Content-Type: application/json" \
  -d '{"name": "phone"}'

# Revoke membership
curl -X DELETE http://localhost:9000/api/v1/buckets/photos/members/<client_id> \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"

# Transfer ownership
curl -X POST http://localhost:9000/api/v1/buckets/photos/owner \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "<new_owner_id>"}'

# List members
curl http://localhost:9000/api/v1/buckets/photos/members \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"
```

### Bucket operations

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/buckets/{bucket}` | Bucket info (owner/member/admin). |
| `POST` | `/api/v1/buckets` | Create a bucket (the creator becomes owner). |
| `DELETE` | `/api/v1/buckets/{bucket}` | Delete the bucket (admin only). |

**Examples:**

```bash
# Create a bucket (the caller becomes owner)
curl -X POST http://localhost:9000/api/v1/buckets \
  -H "Authorization: Bearer hs_<account_id>_<secret>" \
  -d '{"bucket": "documents"}'

# Delete a bucket (admin only)
curl -X DELETE http://localhost:9000/api/v1/buckets/documents \
  -H "Authorization: Bearer hs_HsyncAdminBootstrap01_<secret>"
```

---

## Status Codes and WebSocket Close Codes

### REST

| Code | Meaning |
|---|---|
| `200` | Operation succeeded. |
| `201` | Resource created (bucket, account, membership). |
| `400` | Invalid bucket/account name, malformed payload. |
| `401` | Missing or invalid token. |
| `403` | Authenticated but not permitted (member on an administrative operation, non-admin on `/admin`). |
| `404` | Bucket nonexistent **or** inaccessible (indistinguishable). |
| `409` | Conflict: name taken, last admin, self-target, target already owner/member/disabled. |

### WebSocket

| Close code | Meaning |
|---|---|
| `4000` | Invalid CID, invalid bucket name, CORS, replaced connection. |
| `4001` | First frame is not `auth`. |
| `4002` | Incompatible protocol version. |
| `4003` | Authentication failed (invalid, disabled, expired, revoked token). |
| `4004` | Heartbeat timeout. |
| `4008` | Authentication timeout. |
| `4009` | Bucket not found / no access (indistinguishable). |
| `1013` | Connection limit reached. |

---

## Security Guarantees

1. **Anti-enumeration:** uniform 404/4009 for "nonexistent" and "inaccessible".
2. **Timing-safe:** verifying a nonexistent account runs a "dummy" scrypt of
   identical cost to a real verification.
3. **No secrets in logs:** only `key_prefix` plus id/name.
4. **Immediate revocation:** rotate/disable close WS on the local worker; other
   workers re-verify on every frame, ping and broadcast.
5. **Last admin protected:** the API cannot leave zero active admins.
6. **Token not recoverable:** shown once, never persisted in plaintext.
