# HSYNC Server — Documentation

The HSYNC Server is the central hub for Server Mode synchronization. It is a
FastAPI application that stores files in per-bucket directories, exposes a REST
API for listing/uploading/downloading/deleting files, and pushes change events
to connected clients over WebSocket.

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

## Install & quickstart

Pull the prebuilt multi-arch image (`linux/amd64`, `linux/arm64`) from GHCR and
run it directly — no build required:

```bash
docker pull ghcr.io/albertobasso/hsync-server:latest
docker run -d --name hsync-server \
  -p 8765:8765 \
  -e HSYNC_API_KEY=<your-key> \
  -v "$(pwd)/buckets:/project/buckets" \
  ghcr.io/albertobasso/hsync-server:latest
```

Pin a specific release with a version tag instead of `latest`, e.g.
`ghcr.io/albertobasso/hsync-server:v1.0.0`. Configuration is driven entirely by `HSYNC_*` environment variables.
See the [Environment Variables](#environment-variables) section below for the
full list.

Or build and run the container locally from a checkout:

```bash
cd hsync-server
./docker.sh start          # build + run; see ./docker.sh --help for options
```

Or run it directly from a Poetry environment:

```bash
poetry install
poetry run hsync-server    # honors HSYNC_* env vars (host/port, API key, SSL)
```

Clients then point at it with `-s https://<host>:<port>` and the matching
`HSYNC_API_KEY`. See [Getting Started](../../hsync-client/docs/getting-started.md)
on the client side.

## Documentation map

| Document | What it covers |
| -------- | -------------- |
| [architecture.md](architecture.md) | Server components: FastAPI app, connection manager, storage, hashing. |
| [api.md](api.md) | Complete REST + WebSocket API reference. |
| [sync-flow.md](sync-flow.md) | Server-side sync flow: file storage, event broadcast, tombstones. |
| [faq.md](faq.md) | Frequently asked questions, troubleshooting, and why to choose HSYNC. |
| [deployment.md](deployment.md) | Production deployment: Docker Compose, reverse proxy, systemd, troubleshooting. |
| [operations.md](operations.md) | Day-to-day operations: backup/restore, upgrade, monitoring, performance. |

## Environment Variables

The server is configured entirely through `HSYNC_*` environment variables.
Below is the complete list. See [`dev.env`](../dev.env) for a template with
all defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `HSYNC_PORT` | `8765` | Server listen port |
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
| `HSYNC_RELOAD` | `false` | Enable uvicorn auto-reload (development only) |
| `HSYNC_ENABLE_DOCS` | `false` | Expose `/docs` (Swagger UI) and `/redoc` |

> **Note**: The server does not include a built-in TLS terminator for production
> use. For production deployments, run the server behind a reverse proxy (nginx,
> Caddy, Traefik) that terminates HTTPS. The built-in SSL support
> (`HSYNC_SSL_ENABLED`) generates self-signed certificates suitable for
> development and LAN testing only.

For the ecosystem-wide decision records, see the
[ADRs in hsync-common](../../hsync-common/docs/adr/README.md).

## Upgrade

For upgrade instructions (Docker, Docker Compose, and systemd/Poetry), see the
[Operations Guide → Upgrade Guide](operations.md#upgrade-guide).

## Client usage examples

For practical usage examples of the HSYNC client — including first-time setup,
file exclusion, conflict resolution, restore from tombstones, and migration —
see the [client examples](../../hsync-client/docs/examples.md).

## Related projects

- [hsync-client](../../hsync-client/docs/README.md) — the sync client / CLI.
- [hsync-desktop](../../hsync-desktop/docs/README.md) — desktop GUI front-end.
- [hsync-android](../../hsync-android/docs/README.md) — the Android app.
