# HSYNC Server Architecture

This document describes the architecture of the HSYNC Server.

> The server builds on the shared models, constants, and utilities provided by
> `hsync-common`. See [HSYNC Common Architecture](https://github.com/albertobasso/hsync-common/blob/main/docs/architecture.md)
> for the foundation it depends on, and the
> [Architecture Decision Records](https://github.com/albertobasso/hsync-common/blob/main/docs/adr/README.md) for the
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


> For module structure details and a component reference, see the
> [Developer Guide](developer.md).

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

Once a client has completed the WebSocket handshake (frames and close codes are
documented in [API → Authentication Flow](api.md#authentication-flow)), the
server registers it with the bucket manager and fans out subsequent events to the
other connected clients:

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant WS as WebSocket
    participant BCM as Bucket Manager
    participant FS as File System
    participant C2 as Client 2

    Note over C1,WS: Authenticated (see API → Authentication Flow)
    WS->>BCM: ① Register client

    C1->>WS: ② {"name": "added", "src": {...}}
    WS->>FS: ③ Create file/directory
    WS->>BCM: ④ Get bucket manager
    BCM-->>C2: ⑤ Broadcast event
```
