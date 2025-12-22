# Server Synchronization Flow

This document describes how the HSYNC server handles file synchronization from
its own perspective. The server is a **passive relay**: it stores files,
broadcasts events, and lets clients decide what to do.

> For the client-side flow (initial sync, conflict resolution, event queue,
> retry logic, connection recovery) see the
> [client Sync Flow](https://github.com/albertobasso/hsync-client/blob/main/docs/sync-flow.md).

## Overview

```mermaid
graph TB
    subgraph "Client A"
        CA[HSYNC Client]
    end

    subgraph "HSYNC Server"
        REST[REST API]
        WS[WebSocket Hub]
        FSS[(File Storage)]
    end

    subgraph "Client B"
        CB[HSYNC Client]
    end

    CA -->|"① Upload file"| REST
    REST -->|"② Store"| FSS
    REST -->|"③ Notify"| WS
    WS -.->|"④ Broadcast event"| CB
    CB -->|"⑤ Download file"| REST
    FSS -->|"⑥ Read"| REST
    REST -->|"⑦ File content"| CB
```

## Server responsibilities

The server has three core responsibilities:

1. **Store files** — accept uploads, serve downloads, manage buckets
2. **Broadcast events** — relay change notifications to connected clients
3. **Track deletions** — maintain tombstones so offline peers don't resurrect deleted files

The server does **not**:
- Decide which files to sync (the client decides)
- Decide conflicts by role (uploads are arbitrated **causally** against the
  stored version with a deterministic SHA-256 tie-break on true divergence,
  ADR-0022/ADR-0003 — never by wall clock)
- Queue or retry events (the client retries on disconnect)
- Watch the filesystem (the client watches its local directory)

## File upload flow

```mermaid
sequenceDiagram
    participant C as Client
    participant API as REST API
    participant RL as Rate Limiter
    participant FS as File Storage
    participant HM as Hash Manager
    participant WS as WebSocket
    participant OC as Other Clients

    C->>API: ① POST /api/v1/buckets/{bucket}/files
    API->>RL: ② Check rate limit
    RL-->>API: ③ OK
    API->>HM: ④ Check SHA-256 hash
    alt ⑤a File exists with same hash
        API-->>C: ⑥a 409 Conflict
    else ⑤b New or modified file
        API->>FS: ⑥b Write file atomically
        API->>HM: ⑦ Store SHA-256 hash
        API->>WS: ⑧ Broadcast "added" event
        WS-->>OC: ⑨ Send event
        API-->>C: ⑩ 201 Created
    end
```

## File download flow

```mermaid
sequenceDiagram
    participant C as Client
    participant API as REST API
    participant FS as File Storage

    C->>API: ① GET /api/v1/buckets/{bucket}/files/download?filename={path}
    API->>FS: ② Look up file
    alt ③a File exists
        FS-->>API: ④ File stream
        API-->>C: ⑤ 200 OK + file content
    else ③b File not found
        API-->>C: ⑤ 404 Not Found
    end
```

## WebSocket event broadcast

When a client uploads, modifies, renames, or deletes a file, the server
broadcasts the event to all **other** clients connected to the same bucket:

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant WS as WebSocket
    participant BCM as Bucket Manager
    participant C2 as Client 2
    participant C3 as Client 3

    Note over C1,WS: Client 1 is authenticated
    WS->>BCM: ① Register client for bucket

    C1->>WS: ② Upload complete (REST)
    WS->>BCM: ③ Get bucket connections
    BCM-->>C2: ④ Broadcast event
    BCM-->>C3: ⑤ Broadcast event
```

The server does **not** echo the event back to the originating client (the
originating client already knows about its own change).

## Tombstone flow

Deletion markers (tombstones) prevent offline clients from resurrecting files
they missed being deleted:

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant S as Server
    participant C2 as Client 2 (offline)

    C1->>S: ① DELETE /api/v1/buckets/{bucket}/files
    S->>S: ② Remove file, create tombstone
    S-->>C1: ③ 200 OK

    Note over C2: C2 goes offline, misses the event

    C2->>S: ④ Later: GET /api/v1/buckets/{bucket}/files
    S-->>C2: ⑤ File list (deleted file absent)
    C2->>S: ⑥ GET /api/v1/buckets/{bucket}/tombstones
    S-->>C2: ⑦ Deletion markers
    Note over C2: C2 skips the deleted file during reconciliation
```

## Resumable upload

The server tracks partial uploads so clients can resume interrupted transfers
without re-sending already transferred bytes:

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    C->>S: ① GET .../files/upload-status?filename=big.iso
    S-->>C: ② {"status": "resumable", "offset": 524288}

    C->>S: ③ POST .../files (bytes from offset 524288)
    S->>S: ④ Append to partial file
    S-->>C: ⑤ 201 Created
```

### How it works

1. **Before upload**, the client checks the upload status endpoint. The server
   returns either `{"status": "resumable", "offset": N}` (bytes already
   received) or `{"offset": 0}` (no partial upload exists).

2. **During upload**, the server writes incoming bytes to a partial file
   (`.hsyncpart` suffix) alongside the target path. If the connection drops,
   the partial file preserves the received bytes.

3. **On completion**, the partial file is atomically renamed to the target
   filename. The SHA-256 hash is computed and stored in the cache.

4. **Cleanup**: Partial files left behind after a server restart are removed
   during the next bucket scan. The client also cleans up its own partial
   downloads at the start of each sync cycle.

### Resumable vs. restart

- An upload is **resumable** if the partial file still exists on the server
  and the client's `Content-Range` header matches the stored offset.
- An upload **restarts** if the file was deleted on the server, the partial
  file was cleaned up, or the client's local file hash changed since the
  partial upload began.


## Health and monitoring

The server exposes unauthenticated endpoints for operational monitoring:

- `GET /health` — returns empty `200 OK` (liveness probe)
- `GET /metrics` — Prometheus metrics
- `GET /api/v1/time` — server timestamp for client clock-sync

## See also

- [API Reference](api.md) — complete REST + WebSocket contract.
- [Architecture](internal/architecture.md) — server components and module structure.
- [FAQ](faq.md) — frequently asked questions.
