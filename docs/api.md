# HSYNC Server API Reference

This document provides a complete reference for the HSYNC Server REST and WebSocket APIs.

## API Overview

```mermaid
graph LR
    subgraph Public["① Public Endpoints"]
        H["GET /health"]
        M["GET /metrics"]
        T["GET /api/v1/time"]
        WS["WS .../buckets/:bucket/events/:cid"]
    end

    subgraph Auth["② Authenticated Endpoints"]
        subgraph Buckets["③ Buckets"]
            GB["GET .../buckets/:bucket"]
            CB["POST .../buckets"]
            DB["DELETE .../buckets/:bucket"]
        end

        subgraph Files["④ Files"]
            LF["GET .../buckets/:bucket/files"]
            UF["POST .../buckets/:bucket/files"]
            DF["GET .../files/download"]
            RF["DELETE .../files"]
            US["GET .../files/upload-status"]
            SL["POST .../buckets/:bucket/symlinks"]
        end

        subgraph Tombstones["⑤ Tombstones"]
            GT["GET .../buckets/:bucket/tombstones"]
            PT["POST .../buckets/:bucket/tombstones"]
        end
    end
```

## Authentication

All authenticated endpoints require the `Authorization` header with an account
token:

```
Authorization: Bearer hs_<account_id>_<secret>
```

The token identifies an account with a role (`admin` or `client`) and bucket
memberships. See [Roles and Permissions](roles-and-permissions.md) for the full
access-control model. The admin endpoints (`/api/v1/admin/*`) additionally
require the `admin` role.

## REST API Endpoints

> Rate limits are per-IP defaults and configurable via environment variables
> (see [README → Environment Variables](README.md#environment-variables)).

### Buckets

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}` | Get bucket information | 500/min |
| `POST` | `/api/v1/buckets?bucket={name}` | Create a new bucket | 500/min |
| `DELETE` | `/api/v1/buckets/{bucket}` | Delete a bucket | 500/min |

### Membership and Ownership

Membership endpoints are scoped to a bucket and enforce the owner/admin
permissions described in [Roles and Permissions](roles-and-permissions.md).

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/members` | List members (owner/admin) | 500/min |
| `POST` | `/api/v1/buckets/{bucket}/members` | Grant membership (owner/admin) | 500/min |
| `DELETE` | `/api/v1/buckets/{bucket}/members/{client_id}` | Revoke membership (owner/admin) | 500/min |
| `POST` | `/api/v1/buckets/{bucket}/owner` | Transfer ownership or reclaim an orphan (owner/admin) | 500/min |

### Admin

Admin endpoints require the `admin` role. See
[Roles and Permissions](roles-and-permissions.md) for account and key
lifecycle.

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/admin/clients` | List all accounts | 20/min |
| `POST` | `/api/v1/admin/clients` | Create an account (returns the token once) | 20/min |
| `POST` | `/api/v1/admin/clients/{client_id}/rotate-key` | Rotate an account's key | 20/min |
| `PATCH` | `/api/v1/admin/clients/{client_id}/expiry` | Set/extend/remove key expiry | 20/min |
| `POST` | `/api/v1/admin/clients/{client_id}/disable` | Disable an account | 20/min |
| `POST` | `/api/v1/admin/clients/{client_id}/enable` | Re-enable an account | 20/min |
| `DELETE` | `/api/v1/admin/clients/{client_id}` | Delete an account (`?cascade=true`) | 20/min |
| `GET` | `/api/v1/admin/buckets` | List all buckets (FS ∪ membership) | 20/min |
| `POST` | `/api/v1/admin/buckets` | Create a bucket on behalf of an owner | 20/min |

### Files

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/files` | List files in bucket | 500/min |
| `POST` | `/api/v1/buckets/{bucket}/files` | Upload a file (resumable) | 200/min |
| `GET` | `/api/v1/buckets/{bucket}/files/download?filename={path}` | Download a file | 1000/min |
| `GET` | `/api/v1/buckets/{bucket}/files/upload-status?filename={path}` | Check resumable-upload status (offset) | 500/min |
| `PATCH` | `/api/v1/buckets/{bucket}/files/permissions?filename={path}&permissions={octal}` | Update a file's permissions only (no content upload) | 500/min |
| `GET` | `/api/v1/buckets/{bucket}/dirs` | List directory permissions in the bucket | 500/min |
| `DELETE` | `/api/v1/buckets/{bucket}/files` | Delete a file | 500/min |

### Presence

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/presence` | Group presence: `{connected, total}` clients (non-admin members, self included) | 500/min |

The same pair is pushed to connected clients as a WebSocket `presence` frame
on every join/leave; in LAN mode the master advertises its `connected` count
via the mDNS TXT record.

### Tombstones

Deletion markers let peers that were offline at deletion time avoid resurrecting
files during a full reconciliation.

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/tombstones` | List deletion markers for a bucket | 500/min |
| `POST` | `/api/v1/buckets/{bucket}/tombstones` | Merge deletion markers pushed by a peer | 500/min |

### Delta Transfer

Delta transfer endpoints enable rsync-style block-level synchronization for
efficient file updates. Instead of transferring entire files, only the changed
blocks are sent, significantly reducing bandwidth usage for large files with
small changes.

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/files/delta/signatures?path={path}` | Get block signatures of a file (for upload delta) | 100/min |
| `POST` | `/api/v1/buckets/{bucket}/files/delta/signatures` | Match client signatures against server file (for download delta) | 100/min |
| `POST` | `/api/v1/buckets/{bucket}/files/delta/apply` | Apply a computed delta to update a file | 100/min |
| `GET` | `/api/v1/buckets/{bucket}/files/delta/data/{token}` | Download literal payload for a delta operation | 100/min |

**Concurrency**: Delta apply operations on the same file are automatically
serialized using per-file locks. If multiple clients attempt to apply deltas
to the same file concurrently, they will be processed sequentially. The first
operation to complete will succeed; subsequent operations will receive a 409
Conflict response if the base file has changed, allowing them to retry with
updated signatures.

See [Delta Transfer Guide](delta-transfer.md) for detailed usage examples and
best practices.

### Symlinks

Symlinks are **replicated structurally**: the pair (path, target) travels the
sync channel and the receiving peer reconstructs the link itself (WS `symlink`
event or `POST /symlinks`). On the REST surface the semantics are
per-operation:

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `POST` | `/api/v1/buckets/{bucket}/symlinks?path={path}&target={target}&cid={cid}` | Create or update a symlink entry | 500/min |

> The link is created from the structural pair: `path` is the bucket-relative
> link location, `target` is what it points to. A target escaping the bucket
> is rejected; an existing file/link at `path` is replaced (a non-empty
> directory is refused).

| Operation | Behaviour |
|-----------|-----------|
| List (`GET /files`) | A symlink whose target stays inside the bucket is listed; one whose target escapes the bucket is skipped (its metadata/hash never leak). |
| Download (`GET /files/download`) | A symlink whose target stays inside the bucket is served transparently (static-server semantics); one whose target escapes the bucket is refused with `400`. |
| Delete (`DELETE /files`) | Removes the **link** (the directory entry), never the target it points to. |
| Create/update (`POST /symlinks`, WS event) | Recreates the **link**; a target escaping the bucket is rejected. |
| Delta apply (`POST /delta/apply`) | Refuses symlink paths with `400`: a delta patches file content and never turns a symlink into a regular file. |

### Monitoring & Sync

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `GET` | `/health` | Health check (empty `200 OK`) | No |
| `GET` | `/metrics` | Prometheus metrics | No |
| `GET` | `/api/v1/time` | Server timestamp for client clock sync | No |

## Request/Response Examples

### Upload File

```http
POST /api/v1/buckets/mybucket/files HTTP/1.1
Authorization: Bearer hs_<account_id>_<secret>
Content-Type: multipart/form-data

cid=Client123
file=@document.pdf
path=documents/
ctime=2024-01-15T10:30:00Z
mtime=2024-01-15T10:30:00Z
sha256sum=bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff
permissions=-rw-rw-r--
vclock={"Client123":{"counter":1}}
version_id=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6
corrected_mtime=1737072000
```

> `sha256sum` is **required**: the server verifies the received bytes against
> it and rejects the upload (400) on mismatch. Internal sync-engine filenames
> (`.hsyncignore`, `.hsyncfolder`, leftover `.hsyncstate` / `.hsynctombstones`,
> `*.part`) are rejected,
> and `path`/`filename` must resolve inside the target bucket (no `..`,
> absolute paths or backslashes). Compressed uploads (`compressed=true`) must
> declare the real file `size` and are capped at it when decompressed.

**Response (201 Created):**
```json
{
  "info": "File uploaded successfully",
  "file": "mybucket/documents/document.pdf",
  "resolution": "accepted",
  "version": {
    "vclock": {"Client123": 1, "Client456": 1},
    "corrected_mtime": 1737072000
  }
}
```

> `resolution` is `"accepted"` (causally ahead) or `"accepted_conflict_won"`
> (true concurrent divergence won by the incoming content: the most recent
> offset-corrected `corrected_mtime` wins the chronological tie-break
> (ADR-0023), with a deterministic SHA-256 fallback when a side lacks the
> field; the previous bytes are preserved as a
> `_conflicted_copy_<timestamp>_<cid>` sibling). The `version` object echoes
> the recorded causal version (merged vclock + corrected mtime) so the
> uploader can mirror the agreed base. `vclock`, `version_id` and
> `corrected_mtime` are optional: older peers that omit them are treated as
> an empty clock / unknown mtime and never cause an error. Malformed `vclock`
> payloads are also treated as absent, and a peer-supplied `corrected_mtime`
> farther than 300 s in the future is clamped (conflict engineering).

### List Files

```http
GET /api/v1/buckets/mybucket/files HTTP/1.1
Authorization: Bearer hs_<account_id>_<secret>
```

**Response (200 OK):**
```json
[
  {
    "filepath": "documents/file1.txt",
    "name": "file1.txt",
    "size": 1024,
    "mtime": "2024-01-15T10:30:00Z",
    "ctime": "2024-01-15T10:00:00Z",
    "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
    "permissions": "-rw-rw-r--",
    "version": {
      "vclock": {"Client123": 3},
      "version_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
      "origin_cid": "Client123",
      "ts": 1737072000,
      "corrected_mtime": 1737072000
    }
  }
]
```

> **Note**: when an identical file already exists (same SHA-256), the upload
> endpoint responds `409 Conflict` with `{"info": "File already present", ...}`
> instead of re-uploading the content.
>
> **Causal upload outcomes (`409`):** the server arbitrates every upload against
> its stored version for the path (it is a transport hub, not an authority):
>
> - `409` `{"error": "HSYNC-4005", "resolution": "superseded", ...}` — the upload
>   is causally dominated (a stale ancestor of the stored version): rejected,
>   no conflict copy created (nothing is lost).
> - `409` `{"error": "HSYNC-4006", "resolution": "conflict_lost", ...}` — true
>   concurrent divergence won by the stored version on the chronological
>   tie-break (most recent offset-corrected `corrected_mtime`, SHA-256
>   fallback when a side lacks the field): the incoming bytes are preserved
>   as a `_conflicted_copy_` sibling and the client should download the winner.

### Upload Status (resumable uploads)

```http
GET /api/v1/buckets/mybucket/files/upload-status?filename=documents/big.iso&sha256sum=...&size=1048576 HTTP/1.1
Authorization: Bearer hs_<account_id>_<secret>
```

**Response (200 OK):**
```json
{
  "status": "resumable",
  "offset": 524288,
  "message": "Can resume from byte 524288"
}
```

`status` is one of `new`, `resumable`, or `complete`. The client resumes the
upload from `offset` bytes.

### Tombstones

```http
GET /api/v1/buckets/mybucket/tombstones HTTP/1.1
Authorization: Bearer hs_<account_id>_<secret>
```

**Response (200 OK):** mapping of hashed bucket-relative path → deletion marker.
```json
{
  "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08": {
    "sha256": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
    "version_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "origin_cid": "TestClientX1234567890",
    "ts": 1737000000,
    "vclock": {"TestClientX1234567890": 3}
  }
}
```

`POST` to the same endpoint with a payload of the causal marker shape
(`{"sha256", "version_id", "origin_cid", "ts", "vclock"}`) to merge markers
pushed by a peer: dominance decides, the deletion timestamp never does
(ADR-0003).

### Server Time

```http
GET /api/v1/time HTTP/1.1
```

**Response (200 OK):**
```json
{
  "timestamp": 1737000000.123
}
```

Unix epoch seconds. Clients use this for NTP-like clock-offset estimation.

## WebSocket API

### Endpoint

```
ws(s)://host:9753/api/v1/buckets/{bucket}/events/{cid}
```

> Use `wss://` when the server is behind TLS (recommended for production).
> The client automatically upgrades to `wss://` when the server URL uses
> `https://`.

### Authentication Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    C->>S: ① WebSocket Connect
    S->>C: ② Connection Accepted
    C->>S: ③ {"type": "auth", "token", "app_version", "protocol_version", "capabilities"}
    alt ④a Valid token + matching protocol
        S->>C: ⑤ {"type": "auth", "status": "ok", "protocol_version", "app_version", "capabilities"}
        Note over C,S: ⑥ Client is now authenticated
    else ④b Invalid token
        S->>C: Close (4003: Invalid API key)
    else ④c Protocol mismatch
        S->>C: {"type": "auth", "status": "error", "error": "protocol_incompatible", ...} + Close (4002)
    end
```

The first frame must follow the shared handshake contract
(`hsync_common.protocol.handshake`): `{"type": "auth", "token": "<account_token>",
"app_version": "...", "protocol_version": "<peer protocol>", "capabilities": [...]}`.
The server hard-fails on a `protocol_version` mismatch (sending an
`auth`/`error` reply first) and otherwise responds with its own
`protocol_version`, `app_version` and `capabilities`. The capabilities are the
feature registry (compression, delta, tombstones, vector_clocks); both sides
use the intersection for transfer gating.

**WebSocket close codes:**

| Code | Meaning |
|------|---------|
| `4000` | Invalid client ID or bucket name; connection replaced by a newer one |
| `4001` | Invalid auth message (first frame was not a valid `auth` message) |
| `4002` | Version incompatible (client/server protocol mismatch) |
| `4003` | Invalid token / authentication failed (invalid, disabled, expired, revoked) |
| `4004` | Heartbeat timeout (no pong received within the timeout window) |
| `4008` | Authentication timeout (no auth frame within the timeout window) |
| `4009` | Bucket not found / no access (indistinguishable) |
| `1013` | Connection limit reached (global or per-IP cap exceeded) |

### Event Types

| Event | Description | Payload |
|-------|-------------|---------|
| `added` | File/directory created | `src: File\|Dir` |
| `modified` | File/directory modified | `src: File\|Dir` |
| `renamed` | File/directory renamed | `src: File\|Dir, dst: File\|Dir` |
| `deleted` | File/directory deleted | `src: File\|Dir` |
| `symlink` | Symbolic link created | `src: Symlink` |

> File payloads advertised as `vector_clocks`-capable carry an optional
> `version` field (causal metadata: `vclock`, `version_id`, `origin_cid`,
> `ts`, `corrected_mtime` — ADR-0022), mirroring the `/files` listing.

### Event Message Format

```json
{
  "cid": "TestClientX1234567890",
  "bucket": "mybucket",
  "name": "added",
  "src": {
    "type": "file",
    "filepath": "documents/file.txt",
    "name": "file.txt",
    "size": 1024,
    "mtime": "2024-01-15T10:30:00Z",
    "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff"
  }
}
```

## Error Responses

### HTTP Status Codes

| Status | Description |
|--------|-------------|
| 400 | Bad Request - Invalid parameters or validation error |
| 401 | Unauthorized - Invalid token |
| 403 | Forbidden - Insufficient permissions (non-admin on `/admin`, member on an administrative operation) |
| 404 | Not Found - Resource not found or inaccessible (indistinguishable) |
| 409 | Conflict — file already present (same SHA-256), stale upload (`superseded`), tie-break loser (`conflict_lost`), delta base changed, or an auth conflict (name taken, last admin, self-target) |
| 422 | Unprocessable Entity - Request body validation failed |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |
| 507 | Insufficient Storage - Not enough free disk space for upload |

### Error Response Format

All error responses raised through the server's exception hierarchy follow
this standardized JSON format:

```json
{
  "error": "HSYNC-1001",
  "message": "Human-readable error description",
  "details": {
    "field": "additional context"
  }
}
```

> **Exception: the delta-transfer endpoints** (`/files/delta/*`) return
> FastAPI-style `{"detail": ...}` bodies for their endpoint-specific failures
> (e.g. `404 {"detail": "Not Found"}` when the feature is disabled, `409`
> `{"detail": "base_file_changed", ...}` on a base race, `422`
> `{"detail": "<reason>"}` on invalid instructions or a failed
> reconstruction). Errors raised through the shared exception hierarchy
> (invalid bucket/path, checksum, not found, superseded, conflict-lost)
> keep the standardized `HSYNC-XXXX` format above.

### Error Codes Reference

Errors are categorized by prefix:

| Prefix | Category | Description |
|--------|----------|-------------|
| `HSYNC-1XXX` | Validation | Invalid input data |
| `HSYNC-2XXX` | Not Found | Resource does not exist |
| `HSYNC-3XXX` | Authentication | Auth/authorization failures |
| `HSYNC-4XXX` | File Operations | File operation failures |
| `HSYNC-5XXX` | Server | Internal server errors |

#### Validation Errors (1XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-1000` | ValidationError | Generic validation error |
| `HSYNC-1001` | InvalidBucketNameError | Bucket name contains invalid characters |
| `HSYNC-1002` | InvalidPathError | Path is invalid or contains traversal |
| `HSYNC-1003` | InvalidPermissionFormatError | Permission format is invalid |
| `HSYNC-1005` | ChecksumMismatchError | SHA-256 checksum verification failed |
| `HSYNC-1006` | FileTooLargeError | File exceeds maximum allowed size |
| `HSYNC-1007` | InvalidFilenameError | Filename is invalid or reserved |

#### Resource Not Found Errors (2XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-2000` | ResourceNotFoundError | Generic resource not found |
| `HSYNC-2001` | BucketNotFoundError | Bucket does not exist (or is inaccessible) |
| `HSYNC-2002` | FileNotFoundError | File does not exist |
| `HSYNC-2003` | AccountNotFoundError | Account referenced by id or name does not exist |

#### Authentication Errors (3XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-3000` | AuthenticationError | Generic authentication failure |
| `HSYNC-3001` | InvalidApiKeyError | Token is invalid |
| `HSYNC-3002` | MissingApiKeyError | Token was not provided |
| `HSYNC-3003` | ForbiddenError | Authenticated but not permitted |
| `HSYNC-3004` | ConflictError | Auth conflict (name taken, last admin, self-target, target already owner/member/disabled) |

#### File Operation Errors (4XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-4000` | FileOperationError | Generic file operation failure |
| `HSYNC-4001` | UploadError | File upload failed |
| `HSYNC-4003` | DeleteError | File deletion failed |
| `HSYNC-4004` | InsufficientStorageError | Insufficient storage space on server |
| `HSYNC-4005` | SupersededError | Upload causally dominated by the stored version (stale) |
| `HSYNC-4006` | ConflictLostError | Upload lost the deterministic tie-break; content preserved as conflicted copy |

#### Server Errors (5XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-5000` | HSyncError | Unexpected internal error |

### Example Error Responses

**Invalid Token (401):**
```json
{
  "error": "HSYNC-3001",
  "message": "Invalid API key"
}
```

**Bucket Not Found (404):**
```json
{
  "error": "HSYNC-2001",
  "message": "Bucket not found",
  "details": {
    "bucket": "nonexistent-bucket"
  }
}
```

**Validation Error (422):**
```json
{
  "error": "HSYNC-1000",
  "message": "Validation error",
  "details": {
    "validation_errors": [
      {
        "type": "missing",
        "loc": ["body", "cid"],
        "msg": "Field required"
      }
    ]
  }
}
```
