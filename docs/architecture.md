# HSYNC Server Architecture

This document describes the architecture of the HSYNC Server.

> The server builds on the shared models, constants, and utilities provided by
> `hsync-common`. See [HSYNC Common Architecture](../../hsync-common/docs/architecture.md)
> for the foundation it depends on, and the
> [Architecture Decision Records](../../hsync-common/docs/adr/README.md) for the
> *why* behind the ecosystem's stable decisions.

## System Overview

```mermaid
graph TB
    subgraph "① Clients"
        C1[Client 1]
        C2[Client 2]
        C3[Client N]
    end

    subgraph "② HSYNC Server"
        subgraph "③ FastAPI Application"
            REST[REST API]
            WS[WebSocket Handler]
            RL[Rate Limiter]
        end

        subgraph "④ Core Services"
            CM[Connection Manager]
            BCM[Bucket Connection Manager]
            HM[Hash Manager]
        end

        subgraph "⑤ Storage"
            FS[(File System)]
            SHA-256[(SHA-256 Cache)]
        end
    end

    C1 -->|"① HTTP/REST"| REST
    C2 -->|"① HTTP/REST"| REST
    C1 -->|"② WebSocket"| WS
    C2 -->|"② WebSocket"| WS
    C3 -->|"② WebSocket"| WS

    REST -->|"③"| RL
    RL -->|"④"| HM
    HM -->|"⑤"| FS
    HM -->|"⑥"| SHA-256

    WS -->|"⑦"| BCM
    BCM -->|"⑧"| CM
    CM -.->|"⑨ Broadcast"| C1
    CM -.->|"⑨ Broadcast"| C2
    CM -.->|"⑨ Broadcast"| C3
```

## Component Description

| Component | Description |
|-----------|-------------|
| **REST API** | Handles file upload, download, listing, and bucket management |
| **WebSocket Handler** | Manages real-time event synchronization between clients |
| **Rate Limiter** | Protects endpoints from abuse (slowapi) |
| **Connection Manager** | Manages WebSocket connections per bucket |
| **Bucket Connection Manager** | Isolates connections by bucket for scalability |
| **Hash Manager** | Caches SHA-256 hashes for duplicate detection |
| **File System** | Stores bucket data in `/buckets/{bucket_name}/` |

## Module Structure

```mermaid
graph LR
    subgraph Core["① hsync_server"]
        main[main.py]
        config[config.py]
        routers[routers.py]
        hashes[hashes.py]
        rate_limiter[rate_limiter.py]
        middleware[middleware.py]
        error_handlers[error_handlers.py]
        compression[compression.py]
        tombstones[tombstones.py]

        subgraph Api["② api/"]
            events[events.py]
            event_handlers[event_handlers.py]
            buckets[buckets.py]
            health[health.py]
            connmgr[connection_manager.py]
            utils[utils.py]
            file_utils[file_utils.py]

            subgraph FilesPkg["③ api/files/"]
                flist[list.py]
                fupload[upload.py]
                fdownload[download.py]
                fdelete[delete.py]
            end
        end

        subgraph Models["④ models/"]
            file[file.py]
            dir[dir.py]
            event[event.py]
            symlink[symlink.py]
            sync_action[sync_action.py]
        end
    end

    main --> config
    main --> routers
    main --> rate_limiter
    main --> middleware
    main --> error_handlers
    main --> events
    main --> FilesPkg
    main --> buckets
    main --> health

    FilesPkg --> utils
    FilesPkg --> file_utils
    FilesPkg --> hashes
    FilesPkg --> compression
    FilesPkg --> tombstones
    FilesPkg --> connmgr

    events --> event_handlers
    events --> connmgr
    event_handlers --> Models
```

> Tombstone REST endpoints (`GET`/`POST .../tombstones`) live in `api/files/list.py`
> and use the shared `tombstones.py` store; `health.py` also serves `/api/v1/time`.

## Data Flow

### File Upload Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant API as REST API
    participant RL as Rate Limiter
    participant FS as File System
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
        API->>FS: ⑥b Write file
        API->>HM: ⑦ Store SHA-256 hash
        API->>WS: ⑧ Broadcast "added" event
        WS-->>OC: ⑨ Send event
        API-->>C: ⑩ 201 Created
    end
```

### WebSocket Event Flow

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant WS as WebSocket
    participant BCM as Bucket Manager
    participant FS as File System
    participant C2 as Client 2

    C1->>WS: ① Connect
    WS->>C1: ② Accept
    C1->>WS: ③ {"type": "auth", "token": "..."}
    WS->>C1: ④ {"type": "auth", "status": "ok"}
    WS->>BCM: ⑤ Register client

    C1->>WS: ⑥ {"name": "added", "src": {...}}
    WS->>FS: ⑦ Create file/directory
    WS->>BCM: ⑧ Get bucket manager
    BCM-->>C2: ⑨ Broadcast event
```
