# HSYNC Server — Documentation

The HSYNC Server is the central hub for Server Mode real-time synchronization.
It is a FastAPI application that stores files in per-bucket directories, exposes
a REST API for listing/uploading/downloading/deleting files, and pushes change
events to connected clients over WebSocket.

## System Requirements

### Minimum

| Resource | Value |
|----------|-------|
| CPU | 1 core (x86_64 or arm64) |
| RAM | 256 MB |
| Disk | 1 GB free (plus storage for your files) |
| OS | Linux (kernel ≥ 4.15), macOS, or Windows (WSL2) |

### Recommended

| Resource | Value |
|----------|-------|
| CPU | 2 cores |
| RAM | 1 GB |
| Disk | SSD, with at least 10% free space above your expected storage needs |

### Metadata overhead

The server stores files as plain files on disk — there is no database. The only
additional metadata is:

- **SHA-256 cache**: an in-memory LRU cache (not persisted). Size depends on the
  number of unique files uploaded since the last restart. Roughly 32 bytes per
  entry plus key overhead.
- **Tombstones**: one small JSON file per bucket, typically a few KB even for
  thousands of deletion markers.

Neither should exceed a few MB in practice.

For detailed performance and scalability guidelines, see the
[Operations Guide → Performance and Limits](operations.md#performance-and-limits).

## Start here

- **Private LAN:** follow the single [Getting Started guide](getting-started.md).
- **Public production server:** follow the [Deployment Guide](deployment.md).
- **Manual or development setup:** choose from
  [Installation Alternatives](installation-alternatives.md).

The quick-start commands live only in `getting-started.md` so installation
instructions cannot drift between this index and the project README.

## Where to start

- **New to HSYNC?** → Start with the [Architecture overview](internal/architecture.md).
- **Deploying for the first time?** → Go to the [Deployment Guide](deployment.md).
- **Already running?** → See [Operations Guide](operations.md) for day-to-day tasks.
- **Hardening a production instance?** → Follow the [Security Best Practices](security-best-practices.md) checklist.
- **Integrating with the API?** → Read the [API Reference](api.md).
- **Contributing to the project?** → Start with the [Developer Guide](internal/developer.md).

## Documentation map

### Getting Started & Deployment

| Document | What it covers |
| -------- | -------------- |
| [getting-started.md](getting-started.md) | Canonical private-LAN installation and first connection. |
| [installation-alternatives.md](installation-alternatives.md) | Manual Compose, direct Docker, source build, and Poetry. |
| [deployment.md](deployment.md) | Production deployment: Docker Compose, systemd service, troubleshooting. |
| [reverse-proxy.md](reverse-proxy.md) | Reverse proxy configuration: Nginx and Traefik examples. |

### Day-to-Day Operations

| Document | What it covers |
| -------- | -------------- |
| [operations.md](operations.md) | Backup/restore, upgrade, monitoring, performance. |
| [security-best-practices.md](security-best-practices.md) | Hardening checklist: API keys, firewall, TLS, rate limiting, logging. |
| [faq.md](faq.md) | Frequently asked questions, troubleshooting, and why to choose HSYNC. |
| [log-labels.md](log-labels.md) | Every action label that can appear in the server logs and what it means. |

### Technical Reference

| Document | What it covers |
| -------- | -------------- |
| [api.md](api.md) | Complete REST + WebSocket API reference. |
| [sync-flow.md](sync-flow.md) | Server-side sync flow: file storage, event broadcast, tombstones. |
| [delta-transfer.md](delta-transfer.md) | Rsync-style block sync protocol — algorithm, endpoints, and configuration. |

### Internal / For Contributors

| Document | What it covers |
| -------- | -------------- |
| [internal/architecture.md](internal/architecture.md) | Server components: FastAPI app, connection manager, storage, hashing. |
| [internal/developer.md](internal/developer.md) | Internal implementation details: module structure and component reference. |
| [internal/testing.md](internal/testing.md) | Test suite guide: structure, fixtures, mocking, contract tests. |

## Environment Variables

The server is configured entirely through `HSYNC_*` environment variables.
Below is the complete list. See [`dev.env`](../dev.env) for a template with
all defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `HSYNC_PORT` | `9753` | Server listen port |
| `HSYNC_WORKERS` | `1` | Number of worker processes (must be `1` — WebSocket state is not shared) |
| `HSYNC_LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warning`, `error` |
| `HSYNC_TZ` | `UTC` | Timezone (IANA format, e.g. `Europe/Rome`) |
| `HSYNC_BUCKETS` | `./buckets` | Directory for bucket data storage |
| `HSYNC_API_KEY` | *(required)* | API key for client authentication |
| `HSYNC_MAX_UPLOAD_SIZE` | `10737418240` (10 GiB) | Maximum upload size in bytes |
| `HSYNC_MIN_FREE_DISK_SPACE` | `524288000` (500 MiB) | Minimum free disk before rejecting uploads (`0` = disabled) |
| `HSYNC_CORS_ENABLED` | `false` | Enable CORS headers |
| `HSYNC_CORS_ORIGINS` | — | Allowed CORS origins (comma-separated) |
| `HSYNC_RATE_LIMIT_DEFAULT` | `500/minute` | Default per-IP rate limit |
| `HSYNC_RATE_LIMIT_UPLOAD` | `200/minute` | Upload endpoint rate limit |
| `HSYNC_RATE_LIMIT_DOWNLOAD` | `1000/minute` | Download endpoint rate limit |
| `HSYNC_RATE_LIMIT_WEBSOCKET` | `50/minute` | WebSocket connection rate limit |
| `HSYNC_RATE_LIMIT_WS_MESSAGES` | `10/second` | WebSocket message rate limit |
| `HSYNC_TRUSTED_PROXIES` | — | Comma-separated reverse-proxy IPs for `X-Forwarded-For` |
| `HSYNC_WS_HEARTBEAT_INTERVAL` | `30` | WebSocket heartbeat interval (seconds) |
| `HSYNC_WS_HEARTBEAT_TIMEOUT` | `10` | WebSocket heartbeat timeout (seconds) |
| `HSYNC_WS_AUTH_TIMEOUT` | `10` | WebSocket authentication timeout (seconds) |
| `HSYNC_WS_MAX_CONNECTIONS` | `1000` | Maximum total WebSocket connections |
| `HSYNC_WS_MAX_CONNECTIONS_PER_IP` | `10` | Maximum WebSocket connections per IP |
| `HSYNC_TOMBSTONE_TTL_DAYS` | `30` | Deletion marker retention (days; `0` = forever) |
| `HSYNC_SSL_ENABLED` | `false` | Enable HTTPS/WSS with auto-generated self-signed certs |
| `HSYNC_SSL_KEYFILE_PASSWORD` | — | Password for the SSL private key (required when SSL enabled) |
| `HSYNC_SSL_CERTFILE` | — | Custom SSL certificate path (auto-generated if not set) |
| `HSYNC_SSL_KEYFILE` | — | Custom SSL private key path (auto-generated if not set) |
| `HSYNC_SSL_CERT_DIR` | `./certs` | Directory for auto-generated certificates |
| `HSYNC_LOG_FILE` | — | Path to log file (enables file logging) |
| `HSYNC_LOG_FORMAT` | `text` | Log format: `text` or `json` |
| `HSYNC_LOG_ROTATION` | `10 MB` | Log rotation threshold |
| `HSYNC_LOG_RETENTION` | `7 days` | Log retention period |
| `HSYNC_LOG_COMPRESSION` | `xz` | Compression for rotated logs |
| `HSYNC_COMPRESSION_ENABLED` | `true` | Enable zstd compression for file transfers |
| `HSYNC_DELTA_ENABLED` | `true` | Enable rsync-style delta transfer endpoints |
| `HSYNC_DELTA_MIN_FILE_SIZE` | `262144` (256 KB) | Minimum file size in bytes for delta transfer (files smaller are transferred in full) |
| `HSYNC_DELTA_MAX_CHANGE_RATIO` | `0.50` | Maximum ratio of changed blocks (0.50 = 50%) before delta is rejected |
| `HSYNC_DELTA_TOKEN_TTL` | `300` | Seconds a literal-payload download token stays valid |
| `HSYNC_RATE_LIMIT_DELTA` | `100/minute` | Rate limit for delta transfer endpoints |
| `HSYNC_RELOAD` | `false` | Enable uvicorn auto-reload (development only) |
| `HSYNC_ENABLE_DOCS` | `false` | Expose `/docs` (Swagger UI) and `/redoc` |

> **Note**: The server does not include a built-in TLS terminator for production
> use. For production deployments, run the server behind a reverse proxy (nginx,
> Caddy, Traefik) that terminates HTTPS. The built-in SSL support
> (`HSYNC_SSL_ENABLED`) generates self-signed certificates suitable for
> development and LAN testing only.

For the ecosystem-wide decision records, see the
[ADRs in hsync-common](https://github.com/albertobasso/hsync-common/blob/main/docs/internal/adr/README.md).

## Upgrade

For upgrade instructions (Docker, Docker Compose, and systemd/Poetry), see the
[Operations Guide → Upgrade Guide](operations.md#upgrade-guide).

## Client usage examples

For practical usage examples of the HSYNC client — including first-time setup,
file exclusion, conflict resolution, restore from tombstones, and migration —
see the [client examples](https://github.com/albertobasso/hsync-client/blob/main/docs/examples.md).

## Related projects

- [HSYNC Common](https://github.com/albertobasso/hsync-common/blob/main/README.md) - Shared models, constants, and utilities used by the client
- [HSYNC Client](https://github.com/albertobasso/hsync-client/blob/main/README.md) - Sync client (Server Mode and LAN/P2P Mode)
- [HSYNC Desktop](https://github.com/albertobasso/hsync-desktop/blob/main/docs/README.md) — Desktop client GUI front-end *(in development)*.
- [HSYNC Android](https://github.com/albertobasso/hsync-android/blob/main/docs/README.md) — Android client app *(in development)*.

> **Note:** The desktop GUI (`HSYNC Desktop`) and the Android app (`HSYNC Android`)
> are still under active development and are not yet ready for production use.
