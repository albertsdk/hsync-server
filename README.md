# HSYNC Server

HTTP-based file synchronization server built with FastAPI, featuring WebSocket support for real-time events.

## Features

- 🚀 REST API server for file upload/download
- 📁 Bucket management for file organization
- 🔄 WebSocket for real-time synchronization
- 🔐 API Key authentication (Bearer Token)
- ✅ MD5-based file integrity verification
- 📊 Integrated Prometheus metrics
- 🐳 Docker-ready deployment
- 🛡️ Rate limiting to prevent API abuse
- 🔗 Symbolic link synchronization support
- 📦 Per-bucket connection management for scalability

## Requirements

- Python ≥3.12, <3.14
- Poetry (for dependency management)

## Installation

```bash
# Navigate to the server directory
cd hsync-server

# Install dependencies with Poetry
poetry install

# Start the server
poetry run uvicorn hsync_server.main:app --host 0.0.0.0 --port 8765

# Or run as a module
poetry run python -m hsync_server
```

## Configuration

The server uses environment variables for configuration:

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_API_KEY` | API key for authentication | (empty) |
| `HSYNC_PORT` | Server port | `8765` |
| `HSYNC_LOG_LEVEL` | Log level (`debug`, `info`) | `info` |

## Docker

### Build

```bash
docker build -t hsync:0.1.0 .
```

### Run

```bash
docker run --network="host" \
  -e HSYNC_API_KEY="your-api-key" \
  -e HSYNC_LOG_LEVEL="info" \
  hsync:0.1.0
```

### Debug container

```bash
docker run --rm -it --entrypoint=/bin/sh hsync:0.1.0
```

## REST API

> 📖 For detailed API documentation, see [docs/api.md](docs/api.md)

### Buckets

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/buckets/{bucket}` | Get bucket information |
| `POST` | `/api/v1/buckets?bucket={name}` | Create a new bucket |
| `DELETE` | `/api/v1/buckets/{bucket}` | Delete a bucket |

### Files

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/buckets/{bucket}/files` | List files in bucket |
| `POST` | `/api/v1/buckets/{bucket}/files` | Upload file (multipart/form-data) |
| `GET` | `/api/v1/buckets/{bucket}/files/download?filename={path}` | Download file |
| `DELETE` | `/api/v1/buckets/{bucket}/files` | Delete file |

### Upload Parameters

| Field | Type | Description |
|-------|------|-------------|
| `cid` | string | Client ID |
| `file` | file | File to upload |
| `path` | string | Relative path in bucket |
| `ctime` | string | Creation time |
| `mtime` | string | Modification time |
| `md5sum` | string | MD5 checksum |
| `permissions` | string | File permissions (e.g., `-rw-rw-r--`) |

## WebSocket

### Endpoint

```
ws://host:8765/api/v1/buckets/{bucket}/events/{cid}
```

### Authentication

After connecting, the client must send an authentication message:

```json
{"type": "auth", "token": "your-api-key"}
```

Server responds with:
```json
{"type": "auth", "status": "ok"}
```

If authentication fails, the connection is closed with an appropriate error code.

### Supported Events

| Event | Description |
|-------|-------------|
| `added` | File/directory added |
| `modified` | File/directory modified |
| `renamed` | File/directory renamed |
| `deleted` | File/directory deleted |
| `symlink` | Symbolic link created/modified |

### Message Format

```json
{
  "name": "added|modified|renamed|deleted|symlink",
  "src": { "filepath": "...", "name": "...", ... },
  "dst": { ... }  // only for 'renamed'
}
```

## Rate Limiting

The server implements rate limiting to prevent API abuse:

| Endpoint | Limit |
|----------|-------|
| Upload (`/api/v1/buckets/{bucket}/files` POST) | 50 requests/minute |
| Download (`/api/v1/buckets/{bucket}/files/download`) | 200 requests/minute |
| Files list (`/api/v1/buckets/{bucket}/files` GET) | 100 requests/minute |
| Other endpoints | 100 requests/minute |

When rate limit is exceeded, the server returns HTTP 429 (Too Many Requests).

## Health Check

```bash
curl http://localhost:8765/health
```

## Prometheus Metrics

```bash
curl http://localhost:8765/metrics
```

## Documentation

- [Architecture](docs/architecture.md) - System architecture and component overview
- [API Reference](docs/api.md) - Complete REST and WebSocket API documentation
- [Synchronization Flow](docs/sync-flow.md) - How file synchronization works

## Project Structure

```
hsync-server/
├── hsync_server/           # Main Python package
│   ├── __init__.py
│   ├── __main__.py         # Entry point: python -m hsync_server
│   ├── config.py           # Configuration
│   ├── main.py             # FastAPI application
│   ├── routers.py          # Router configuration
│   ├── hashes.py           # MD5 hash caching
│   ├── rate_limiter.py     # Rate limiting configuration
│   ├── api/                # API endpoints
│   │   ├── __init__.py
│   │   ├── utils.py        # API utilities
│   │   ├── events.py       # WebSocket handler
│   │   ├── files.py        # REST API files/buckets
│   │   └── health.py       # Health check
│   └── models/             # Data models
│       ├── __init__.py
│       ├── dir.py
│       ├── event.py
│       ├── file.py
│       ├── symlink.py      # Symbolic link model
│       └── sync_action.py
├── docs/                   # Documentation with Mermaid diagrams
│   ├── architecture.md
│   ├── api.md
│   └── sync-flow.md
├── config/                 # Configuration files
│   ├── logging-debug.yaml
│   └── logging-info.yaml
├── docker/                 # Docker scripts
├── tests/                  # Test files
├── Dockerfile
├── pyproject.toml
└── poetry.lock
```

## Authentication

All API requests (except `/health` and `/metrics`) require the header:

```
Authorization: Bearer {HSYNC_API_KEY}
```

## Version

0.1.0
