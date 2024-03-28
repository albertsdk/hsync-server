# HSYNC Server API Reference

This document provides a complete reference for the HSYNC Server REST and WebSocket APIs.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

## API Overview

```mermaid
graph LR
    subgraph Public["① Public Endpoints"]
        H["GET /health"]
        M["GET /metrics"]
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
        end
    end
```

![API Overview](images/api-1.png)

## Authentication

All authenticated endpoints require the `Authorization` header:

```
Authorization: Bearer {HSYNC_API_KEY}
```

## REST API Endpoints

### Buckets

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}` | Get bucket information | 100/min |
| `POST` | `/api/v1/buckets?bucket={name}` | Create a new bucket | 100/min |
| `DELETE` | `/api/v1/buckets/{bucket}` | Delete a bucket | 100/min |

### Files

| Method | Endpoint | Description | Rate Limit |
|--------|----------|-------------|------------|
| `GET` | `/api/v1/buckets/{bucket}/files` | List files in bucket | 100/min |
| `POST` | `/api/v1/buckets/{bucket}/files` | Upload a file | 50/min |
| `GET` | `/api/v1/buckets/{bucket}/files/download?filename={path}` | Download a file | 200/min |
| `DELETE` | `/api/v1/buckets/{bucket}/files` | Delete a file | 100/min |

### Monitoring

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `GET` | `/health` | Health check | No |
| `GET` | `/metrics` | Prometheus metrics | No |

## Request/Response Examples

### Upload File

```http
POST /api/v1/buckets/mybucket/files HTTP/1.1
Authorization: Bearer your-api-key
Content-Type: multipart/form-data

cid=client-123
file=@document.pdf
path=documents/
ctime=2024-01-15T10:30:00Z
mtime=2024-01-15T10:30:00Z
md5sum=d41d8cd98f00b204e9800998ecf8427e
permissions=-rw-rw-r--
```

**Response (201 Created):**
```json
{
  "info": "File uploaded successfully",
  "file": "mybucket/documents/document.pdf"
}
```

### List Files

```http
GET /api/v1/buckets/mybucket/files?sync_order=mtime HTTP/1.1
Authorization: Bearer your-api-key
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
    "md5sum": "d41d8cd98f00b204e9800998ecf8427e",
    "permissions": "-rw-rw-r--"
  }
]
```

## WebSocket API

### Endpoint

```
ws://host:8765/api/v1/buckets/{bucket}/events/{cid}
```

### Authentication Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    C->>S: ① WebSocket Connect
    S->>C: ② Connection Accepted
    C->>S: ③ {"type": "auth", "token": "api-key"}
    alt ④a Valid token
        S->>C: ⑤ {"type": "auth", "status": "ok"}
        Note over C,S: ⑥ Client is now authenticated
    else ④b Invalid token
        S->>C: ⑤ Close (4003: Invalid API key)
    end
```

![Authentication Flow](images/api-2.png)

### Event Types

| Event | Description | Payload |
|-------|-------------|---------|
| `added` | File/directory created | `src: File\|Dir` |
| `modified` | File/directory modified | `src: File\|Dir` |
| `renamed` | File/directory renamed | `src: File\|Dir, dst: File\|Dir` |
| `deleted` | File/directory deleted | `src: File\|Dir` |
| `symlink` | Symbolic link created | `src: Symlink` |

### Event Message Format

```json
{
  "cid": "client-123",
  "bucket": "mybucket",
  "name": "added",
  "src": {
    "type": "file",
    "filepath": "documents/file.txt",
    "name": "file.txt",
    "size": 1024,
    "mtime": "2024-01-15T10:30:00Z",
    "md5sum": "d41d8cd98f00b204e9800998ecf8427e"
  }
}
```

## Error Responses

### HTTP Status Codes

| Status | Description |
|--------|-------------|
| 400 | Bad Request - Invalid parameters or validation error |
| 401 | Unauthorized - Invalid API key |
| 403 | Forbidden - Missing API key |
| 404 | Not Found - Resource not found |
| 422 | Unprocessable Entity - Request body validation failed |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |

### Error Response Format

All error responses follow this standardized JSON format:

```json
{
  "error": "HSYNC-1001",
  "message": "Human-readable error description",
  "details": {
    "field": "additional context"
  }
}
```

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
| `HSYNC-1004` | InvalidSyncOrderError | Sync order parameter is invalid |
| `HSYNC-1005` | ChecksumMismatchError | MD5 checksum verification failed |
| `HSYNC-1006` | FileTooLargeError | File exceeds maximum allowed size |
| `HSYNC-1007` | InvalidFilenameError | Filename is invalid or reserved |

#### Resource Not Found Errors (2XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-2000` | ResourceNotFoundError | Generic resource not found |
| `HSYNC-2001` | BucketNotFoundError | Bucket does not exist |
| `HSYNC-2002` | FileNotFoundError | File does not exist |

#### Authentication Errors (3XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-3000` | AuthenticationError | Generic authentication failure |
| `HSYNC-3001` | InvalidApiKeyError | API key is invalid |
| `HSYNC-3002` | MissingApiKeyError | API key was not provided |

#### File Operation Errors (4XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-4000` | FileOperationError | Generic file operation failure |
| `HSYNC-4001` | UploadError | File upload failed |
| `HSYNC-4002` | DownloadError | File download failed |
| `HSYNC-4003` | DeleteError | File deletion failed |

#### Server Errors (5XXX)

| Code | Name | Description |
|------|------|-------------|
| `HSYNC-5000` | HSyncError | Unexpected internal error |

### Example Error Responses

**Invalid API Key (401):**
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

