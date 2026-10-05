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

Synced **file bytes** live as plain files under the buckets directory. Causal
metadata does **not**: versions and tombstones are stored in one SQLite database
**per bucket** (`<state_dir>/server/<bucket>.db`) and accounts/membership in a
global `auth.db`, under the state directory (ADR-0025). Back those files up
together with the buckets directory.

Additional process-local metadata:

- **SHA-256 cache**: an in-memory `dict` (not persisted, **not** LRU). It is
  rebuilt at startup by walking the buckets tree and grows with every hashed
  path for the life of the process. Stale entries are dropped when mtime/size
  no longer match; there is no size cap.
- **Tombstones / versions**: SQLite tables in per-bucket DBs, not per-bucket
  JSON files. Tombstone TTL (default 90 days) only garbage-collects markers.

For detailed performance and scalability guidelines, see the
[Operations Guide → Performance and Limits](operations.md#performance-and-limits).

## Start here

This guide is organised as a linear journey; pick the step that matches where
you are.

### 1. Choose your server

Decide which installation fits you, then follow that method end to end. Methods
are ordered from the simplest to the most involved:

- [Method 1 — Installer script](getting-started-script.md): `install.sh` via
  `curl`, released container image, certificate management. **Best for a host
  with Docker, least setup.**
- [Method 2 — Docker / Compose](getting-started-docker.md): manual Compose,
  direct `docker run`, or a source image build. **Best if you manage
  containers.**
- [Method 3 — Debian/Ubuntu package](getting-started-deb.md): the server
  packaged as a `.deb` (systemd unit, conffile, dedicated `_hsync-server`
  user). **Best for a Debian/Ubuntu host without Docker.**
- [Method 4 — RPM package](getting-started-rpm.md): the server packaged as a
  `.rpm` (systemd unit, conffile, dedicated `_hsync-server` user). **Best for a
  RHEL/Rocky/Alma/Fedora host without Docker.**
- [Method 5 — From source](getting-started-source.md): clone and run with
  `run.sh` / Poetry, or a standalone binary. **Best for development or a host
  you control.**

Start from the [Getting Started](getting-started.md) chooser for a comparison
table that helps you pick.

### 2. Connect your clients

Each client authenticates with an account token (`hs_<id>_<secret>`) in
`HSYNC_API_KEY`. The path you chose shows the exact commands to create the
account, create the bucket, and grant access. The full command reference —
including how to invoke the CLI on each installation type — is in
[Roles and Permissions](roles-and-permissions.md#cli-tools).

### 3. Administrate and operate

| Task | Guide |
| --- | --- |
| Backup, restore, upgrade, monitoring | [Operations Guide](operations.md) |
| Roles, permissions, keys, bucket membership | [Roles and Permissions](roles-and-permissions.md) |
| Public Internet production (reverse proxy, systemd) | [Deployment Guide](deployment.md) |
| Reverse proxy examples (nginx/Traefik) | [Reverse Proxy Guide](reverse-proxy.md) |
| Security hardening checklist | [Security Best Practices](security-best-practices.md) |
| Troubleshooting and FAQ | [FAQ](faq.md) |

### 4. Reference & internals

| Document | What it covers |
| -------- | -------------- |
| [api.md](api.md) | Complete REST + WebSocket contract (endpoints, payloads, error codes). |
| [authentication.md](authentication.md) | Auth flow: WebSocket handshake, account token validation, CID registration. |
| [sync-flow.md](sync-flow.md) | Server-side sync flow: storage, event broadcast, tombstones. |
| [delta-transfer.md](delta-transfer.md) | Rsync-style block sync protocol. |
| [log-labels.md](log-labels.md) | Every action label the server can write to its logs. |
| [internal/architecture.md](internal/architecture.md) | Server internals (FastAPI app, connection manager, storage, hashing). |
| [internal/developer.md](internal/developer.md) | Contributor: module structure and component reference. |
| [internal/testing.md](internal/testing.md) | Contributor: test suite structure, fixtures, contract tests. |

## Environment Variables

The server is configured entirely through `HSYNC_*` environment variables.
Below is the complete list. See [`dev.env`](../dev.env) for a template with
all defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `HSYNC_PORT` | `9753` | Server listen port |
| `HSYNC_WORKERS` | `1` | Number of worker processes (must be `1` — WebSocket state is not shared) |
| `HSYNC_LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warning`, `error` |
| `HSYNC_TZ` | `UTC` | Timezone (IANA format, e.g. `Europe/Rome`) used to render log timestamps |
| `HSYNC_BUCKETS` | `<app>/buckets` (absolute) | Directory for bucket data storage (relative values are resolved against the process CWD) |
| `HSYNC_STATE_DIR` | `<app>/state` (absolute) | Directory for the per-bucket state DBs and the global auth DB |
| `HSYNC_ADMIN_KEY` | *(optional)* | Bootstrap admin secret (64 hex). If unset, the server generates and prints a one-time admin token. |
| `HSYNC_MAX_UPLOAD_SIZE` | `10737418240` (10 GiB) | Maximum upload size in bytes (`0` = unlimited, which also disables the request-body cap below unless it is set explicitly) |
| `HSYNC_MAX_REQUEST_BODY_SIZE` | `0` (auto) | Hard cap in bytes on any HTTP request body, enforced before it is buffered. `0` derives `HSYNC_MAX_UPLOAD_SIZE` + 64 MiB |
| `HSYNC_MIN_FREE_DISK_SPACE` | `524288000` (500 MiB) | Minimum free disk before rejecting uploads (`0` = disabled) |
| `HSYNC_CORS_ENABLED` | `false` | Enable CORS headers |
| `HSYNC_CORS_ORIGINS` | — | Allowed CORS origins (comma-separated) |
| `HSYNC_RATE_LIMIT_DEFAULT` | `500/minute` | Default per-IP rate limit |
| `HSYNC_RATE_LIMIT_UPLOAD` | `200/minute` | Upload endpoint rate limit |
| `HSYNC_RATE_LIMIT_DOWNLOAD` | `1000/minute` | Download endpoint rate limit |
| `HSYNC_RATE_LIMIT_ADMIN` | `20/minute` | Admin endpoint rate limit |
| `HSYNC_RATE_LIMIT_WS_MESSAGES` | `10/second` | WebSocket message rate limit |
| `HSYNC_TRUSTED_PROXIES` | — | Comma-separated reverse-proxy IPs for `X-Forwarded-For` |
| `HSYNC_WS_HEARTBEAT_INTERVAL` | `30` | WebSocket heartbeat interval (seconds) |
| `HSYNC_WS_HEARTBEAT_TIMEOUT` | `10` | WebSocket heartbeat timeout (seconds) |
| `HSYNC_WS_AUTH_TIMEOUT` | `10` | WebSocket authentication timeout (seconds) |
| `HSYNC_WS_MAX_CONNECTIONS` | `1000` | Maximum total WebSocket connections |
| `HSYNC_WS_MAX_CONNECTIONS_PER_IP` | `10` | Maximum WebSocket connections per IP |
| `HSYNC_TOMBSTONE_TTL_DAYS` | `90` | Deletion marker retention (days; `0` = forever). Markers only; not a recycle bin |
| `HSYNC_SSL_ENABLED` | `false` (app default) | Enable HTTPS/WSS with auto-generated self-signed certs. The shipped `dev.env` and `prod.env` profiles set it to `true`, so the Docker quickstart serves **https** on this port |
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
| `HSYNC_DELTA_TOKEN_TTL` | `300` | Seconds a literal-payload download token stays valid |
| `HSYNC_RATE_LIMIT_DELTA` | `100/minute` | Rate limit for delta transfer endpoints |
| `HSYNC_RELOAD` | `false` | Enable uvicorn auto-reload (development only) |
| `HSYNC_ENABLE_DOCS` | `false` (app default) | Expose `/docs` (Swagger UI) and `/redoc`. Set it to `true` in your env-file to browse the API interactively (`prod.env` leaves it off) |

> **Note**: The server does not include a built-in TLS terminator for production
> use. For production deployments, run the server behind a reverse proxy (nginx,
> Caddy, Traefik) that terminates HTTPS. The built-in SSL support
> (`HSYNC_SSL_ENABLED`) generates self-signed certificates suitable for
> development and LAN testing only.

For the ecosystem-wide decision records, see the
[ADRs in hsync-common](https://github.com/albertsdk/hsync-common/blob/main/docs/internal/adr/README.md).

## Upgrade

For upgrade instructions (Docker, Docker Compose, and systemd/Poetry), see the
[Operations Guide → Upgrade Guide](operations.md#upgrade-guide).

## Client usage examples

For practical usage examples of the HSYNC client — including first-time setup,
file exclusion, conflict resolution, restore from tombstones, and migration —
see the [client examples](https://github.com/albertsdk/hsync-client/blob/main/docs/examples.md).

## Associated Ecosystem Components

> The other HSYNC ecosystem components are listed in the
> [project README](../README.md#associated-ecosystem-components).
