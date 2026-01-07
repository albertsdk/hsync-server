# HSYNC Server — Operations Guide

This guide covers day-to-day operations of the HSYNC Server: backup and restore,
upgrade procedures, monitoring, and performance guidelines.

> For deployment instructions (Docker Compose, reverse proxy, systemd) see the
> [Deployment Guide](deployment.md).

---

## Table of Contents

- [Persistent Volumes](#persistent-volumes)
- [Backup and Restore](#backup-and-restore)
- [Upgrade Guide](#upgrade-guide)
- [Monitoring](#monitoring)
- [Performance and Limits](#performance-and-limits)

---

## Persistent Volumes

When running under Docker, the following data must be preserved across container
recreations and upgrades:

| Volume | Container path | Contents |
|--------|---------------|----------|
| `hsync-buckets` | `/project/buckets` | All uploaded files, one sub-directory per bucket |
| `hsync-certs` | `/project/certs` | Auto-generated SSL certificates (if using built-in SSL) |
| `hsync-logs` | `/project/logs` | Log files (if `HSYNC_LOG_FILE` is set) |

> **Note**: Tombstones are stored inside the buckets directory
> (`/project/buckets/.tombstones/`), so they are automatically included in the
> bucket volume backup.

With `docker.sh`, these volumes are managed automatically:
- `hsync-server-certs` — named volume for SSL certificates (persists across
  restarts; removed by `./docker.sh clean`)
- `buckets/` — bind-mounted from the project directory

---

## Backup and Restore

### What to back up

The only data that needs to be backed up is the **buckets directory**. It
contains all uploaded files and tombstones.

### Backup

```bash
#!/bin/bash
# backup-hsync.sh — run daily via cron

BACKUP_DIR="/var/backups/hsync"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"

# Stop the server to ensure consistency (optional but recommended)
./docker.sh stop

# Create a compressed archive of the buckets directory
tar -czf "$BACKUP_DIR/hsync-buckets-$TIMESTAMP.tar.gz" -C buckets .

# Restart the server
./docker.sh start --env-file prod.env

# Keep only the last 30 backups
ls -t "$BACKUP_DIR"/hsync-buckets-*.tar.gz | tail -n +31 | xargs -r rm
```

For a zero-downtime backup (without stopping the container), use a filesystem
snapshot (LVM, ZFS, btrfs) or `docker cp`:

```bash
docker cp hsync-server:/project/buckets /tmp/hsync-buckets-backup
tar -czf "$BACKUP_DIR/hsync-buckets-$TIMESTAMP.tar.gz" -C /tmp hsync-buckets-backup
rm -rf /tmp/hsync-buckets-backup
```

> **Warning**: `docker cp` on a live container is not atomic. For critical data,
> stop the container or use filesystem snapshots.

### Restore

```bash
# Stop the server
./docker.sh stop

# Restore from backup (overwrites the buckets directory)
tar -xzf "$BACKUP_DIR/hsync-buckets-20250101-120000.tar.gz" -C buckets

# Start the server
./docker.sh start --env-file prod.env
```

---

## Upgrade Guide

### Using `docker.sh` (single container)

```bash
# 1. Pull the latest code
git pull

# 2. Rebuild the image
./docker.sh build

# 3. Restart (reuses the env-file from the last start)
./docker.sh restart
```

To pin a specific version tag instead of building locally:

```bash
export HSYNC_VERSION=v1.0.0
./docker.sh build   # builds hsync:v1.0.0
./docker.sh start --env-file prod.env
```

### Docker Compose

```bash
docker compose pull
docker compose up -d
```

### Systemd (Poetry)

```bash
cd /opt/hsync-server
git pull
poetry install --only main
sudo systemctl restart hsync-server
```

### Version pinning

Always pin a specific version tag in production instead of `latest`:

```bash
docker pull ghcr.io/albertobasso/hsync-server:v1.0.0
```

Check the [releases page](https://github.com/albertobasso/hsync-server/releases)
for changelogs and migration notes before upgrading.

---

## Monitoring

### Health check

```bash
curl http://localhost:8765/health
# → 200 OK (empty body)
```

Use this endpoint for container orchestration liveness probes and load balancer
health checks.

### Prometheus metrics

```bash
curl http://localhost:8765/metrics
```

The `/metrics` endpoint exposes standard FastAPI metrics (request count, latency,
response size) via `prometheus-fastapi-instrumentator`. Integrate with your
existing Prometheus + Grafana stack.

### Logging

In production, set `HSYNC_LOG_FORMAT=json` for structured logging that integrates
with log aggregation tools (Loki, ELK, Datadog, etc.):

```json
{"timestamp":"2026-01-07T15:30:00.123456Z","level":"INFO","module":"hsync_server.api.files.upload","message":"File uploaded","bucket":"mybucket","file":"documents/report.pdf","size":1048576}
```

### Log rotation

When file logging is enabled (`HSYNC_LOG_FILE`), the server rotates logs
automatically. Configure rotation with:

| Variable | Default | Description |
|----------|---------|-------------|
| `HSYNC_LOG_ROTATION` | `10 MB` | Rotate when the log file reaches this size |
| `HSYNC_LOG_RETENTION` | `7 days` | Keep rotated logs for this duration |
| `HSYNC_LOG_COMPRESSION` | `xz` | Compression algorithm for rotated logs |

---

## Performance and Limits

### Scalability guidelines

| Aspect | Limit / Guideline |
|--------|-------------------|
| **Buckets per server** | No hard limit; each bucket is a directory. Practical limit: thousands. |
| **Files per bucket** | No hard limit. Listing performance depends on filesystem `readdir` speed. For buckets with >100k files, consider subdirectories. |
| **Concurrent clients** | Up to `HSYNC_WS_MAX_CONNECTIONS` (default 1000). Each WebSocket connection uses ~50–100 KB of RAM. |
| **Max upload size** | Configurable via `HSYNC_MAX_UPLOAD_SIZE` (default 10 GiB). |
| **Concurrent uploads** | Limited by uvicorn worker threads and `HSYNC_WORKERS` (must be 1 for WebSocket support). |

### Large file behavior

- **Files > 1 GB**: Uploads are resumable (offset-based). The server streams
  downloads without loading the entire file into memory.
- **Memory usage**: The server uses `aiofiles` for async I/O and streams file
  content in chunks. A 10 GB upload will not consume 10 GB of RAM.
- **Disk space**: The server checks `HSYNC_MIN_FREE_DISK_SPACE` before accepting
  uploads and rejects them if disk space is too low.

### Network bandwidth

- **Compression**: Zstandard (zstd) compression is enabled by default for
  downloads. Files under 10 KB and already-compressed formats (`.jpg`, `.png`,
  `.mp4`, `.zip`, etc.) are skipped.
- **WebSocket overhead**: Each event message is a small JSON payload (typically
  < 1 KB). The fan-out is proportional to the number of connected clients.

---

## See also

- [Deployment Guide](deployment.md) — Docker Compose, reverse proxy, systemd.
- [README](README.md) — install and quickstart.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [FAQ](faq.md) — frequently asked questions and troubleshooting.
