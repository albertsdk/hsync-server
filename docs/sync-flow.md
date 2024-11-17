# HSYNC Synchronization Flow

This document describes how file synchronization works between clients through the HSYNC server.

## Overview

HSYNC uses a combination of REST API for file transfers and WebSocket for real-time event propagation.

```mermaid
graph TB
    subgraph "Client A"
        FSA[File System]
        WDA[Watchdog]
        CA[HSYNC Client]
    end

    subgraph "HSYNC Server"
        REST[REST API]
        WS[WebSocket Hub]
        FSS[(Server Storage)]
    end

    subgraph "Client B"
        CB[HSYNC Client]
        FSB[File System]
    end

    FSA -->|"① File Change"| WDA
    WDA -->|"② Event"| CA
    CA -->|"③ Upload"| REST
    REST -->|"④ Store"| FSS
    CA -->|"⑤ Event"| WS
    WS -.->|"⑥ Broadcast"| CB
    CB -->|"⑦ Download Request"| REST
    FSS -->|"⑧ Read"| REST
    REST -->|"⑨ File Content"| CB
    CB -->|"⑩ Write"| FSB
```

## Initial Synchronization

When a client starts, it performs a full sync to ensure consistency:

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    C->>S: ① GET /api/v1/buckets/{bucket}/files
    S-->>C: ② List of remote files

    C->>C: ③ Scan local directory
    C->>C: ④ Compare local vs remote

    loop ⑤ For each difference
        alt ⑤a Remote newer
            C->>S: ⑥ GET /api/v1/buckets/{bucket}/files/download
            S-->>C: ⑦ File content
            C->>C: ⑧ Write to local disk
        else ⑤b Local newer
            C->>S: ⑥ POST /api/v1/buckets/{bucket}/files
            S-->>C: ⑦ Upload confirmation
        else ⑤c Only remote
            C->>S: ⑥ GET /api/v1/buckets/{bucket}/files/download
            S-->>C: ⑦ File content
        else ⑤d Only local
            C->>S: ⑥ POST /api/v1/buckets/{bucket}/files
            S-->>C: ⑦ Upload confirmation
        end
    end

    C->>C: ⑨ Sync complete
```

## Real-Time Synchronization (Watch Mode)

In watch mode, clients maintain a WebSocket connection for instant updates:

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant S as Server
    participant C2 as Client 2

    Note over C1,C2: ① Both clients connected via WebSocket

    C1->>C1: ② User creates file.txt
    C1->>S: ③ POST /api/v1/buckets/{bucket}/files
    S->>S: ④ Store file
    S-->>C2: ⑤ WebSocket: {"name": "added", "src": {...}}
    C2->>S: ⑥ GET /api/v1/buckets/{bucket}/files/download
    S-->>C2: ⑦ File content
    C2->>C2: ⑧ Write file.txt locally
```

## Event Types and Actions

```mermaid
flowchart TD
    E[① Event Received] -->|"②"| T{Event Type?}

    T -->|"③a added"| A[Download file]
    T -->|"③b modified"| M[Download file]
    T -->|"③c renamed"| R[Rename local file]
    T -->|"③d deleted"| D[Delete local file]
    T -->|"③e symlink"| L[Create symlink]

    A -->|"④"| V{Verify SHA-256}
    M -->|"④"| V
    V -->|"⑤a Match"| S[Success]
    V -->|"⑤b Mismatch"| RE[Retry download]
```

## Conflict Resolution

HSYNC uses "last write wins" based on modification time:

```mermaid
flowchart TD
    C[① Compare Files] -->|"②"| T{mtime comparison}

    T -->|"③a remote > local"| D[Download from server]
    T -->|"③b local > remote"| U[Upload to server]
    T -->|"③c equal"| H{④ SHA-256 match?}

    H -->|"⑤a Yes"| S[Skip - files identical]
    H -->|"⑤b No"| U2[Upload local version]
```

## Event Queue and Retry

The client uses an event queue with exponential backoff for reliability:

```mermaid
stateDiagram-v2
    [*] --> Queued: ① Add event
    Queued --> Processing: ② Dequeue
    Processing --> Success: ③a Event sent
    Processing --> Retry: ③b Network error
    Retry --> Queued: ④ Backoff wait
    Retry --> Failed: ⑤ Max retries
    Success --> [*]
    Failed --> [*]

    note right of Retry
        Backoff: 1s, 2s, 4s, 8s...
        Max: 60s
        Max retries: 5
    end note
```

## Connection Recovery

When the WebSocket connection is lost:

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    C->>S: ① WebSocket connected
    Note over C,S: ② Connection lost

    C->>C: ③ Wait reconnect_delay seconds
    C->>S: ④ Reconnect WebSocket
    S-->>C: ⑤ Connection accepted
    C->>S: ⑥ Authenticate
    S-->>C: ⑦ Auth OK

    C->>S: ⑧ GET /api/v1/buckets/{bucket}/files
    S-->>C: ⑨ Current file list
    C->>C: ⑩ Resync to catch missed events
```
