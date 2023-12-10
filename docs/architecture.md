# HSYNC Server Architecture

This document describes the architecture of the HSYNC Server.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

## System Overview

```mermaid
graph TB
    subgraph Clients
        C1[Client 1]
        C2[Client 2]
        C3[Client N]
    end

    subgraph "HSYNC Server"
        subgraph "FastAPI Application"
            REST[REST API]
            WS[WebSocket Handler]
            RL[Rate Limiter]
        end
        
        subgraph "Core Services"
            CM[Connection Manager]
            BCM[Bucket Connection Manager]
            HM[Hash Manager]
        end
        
        subgraph "Storage"
            FS[(File System)]
            MD5[(MD5 Cache)]
        end
    end

    C1 -->|HTTP/REST| REST
    C2 -->|HTTP/REST| REST
    C1 -->|WebSocket| WS
    C2 -->|WebSocket| WS
    C3 -->|WebSocket| WS
    
    REST --> RL
    RL --> HM
    HM --> FS
    HM --> MD5
    
    WS --> BCM
    BCM --> CM
    CM -->|Broadcast| C1
    CM -->|Broadcast| C2
    CM -->|Broadcast| C3
```

![System Overview](images/architecture-1.png)

## Component Description

| Component | Description |
|-----------|-------------|
| **REST API** | Handles file upload, download, listing, and bucket management |
| **WebSocket Handler** | Manages real-time event synchronization between clients |
| **Rate Limiter** | Protects endpoints from abuse (slowapi) |
| **Connection Manager** | Manages WebSocket connections per bucket |
| **Bucket Connection Manager** | Isolates connections by bucket for scalability |
| **Hash Manager** | Caches MD5 hashes for duplicate detection |
| **File System** | Stores bucket data in `/buckets/{bucket_name}/` |

## Module Structure

```mermaid
graph LR
    subgraph "hsync_server"
        main[main.py]
        config[config.py]
        routers[routers.py]
        hashes[hashes.py]
        rate_limiter[rate_limiter.py]
        
        subgraph "api/"
            events[events.py]
            files[files.py]
            health[health.py]
            utils[utils.py]
        end
        
        subgraph "models/"
            file[file.py]
            dir[dir.py]
            event[event.py]
            symlink[symlink.py]
        end
    end

    main --> config
    main --> routers
    main --> rate_limiter
    main --> events
    main --> files
    main --> health
    
    files --> utils
    files --> hashes
    files --> events
    
    events --> models/
```

![Module Structure](images/architecture-2.png)

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

    C->>API: POST /api/v1/buckets/{bucket}/files
    API->>RL: Check rate limit
    RL-->>API: OK
    API->>HM: Check MD5 hash
    alt File exists with same hash
        API-->>C: 409 Conflict
    else New or modified file
        API->>FS: Write file
        API->>HM: Store MD5 hash
        API->>WS: Broadcast "added" event
        WS->>OC: Send event
        API-->>C: 201 Created
    end
```

![File Upload Flow](images/architecture-3.png)

### WebSocket Event Flow

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant WS as WebSocket
    participant BCM as Bucket Manager
    participant FS as File System
    participant C2 as Client 2

    C1->>WS: Connect
    WS->>C1: Accept
    C1->>WS: {"type": "auth", "token": "..."}
    WS->>C1: {"type": "auth", "status": "ok"}
    WS->>BCM: Register client

    C1->>WS: {"name": "added", "src": {...}}
    WS->>FS: Create file/directory
    WS->>BCM: Get bucket manager
    BCM->>C2: Broadcast event
```

![WebSocket Event Flow](images/architecture-4.png)
