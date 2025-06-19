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

The recommended Compose installer uses stable named volumes:

- `hsync-server-buckets` — files and tombstones
- `hsync-server-certs` — the persistent self-signed certificate and key

---

## Backup and Restore

### What to back up

The **buckets directory** contains all uploaded files and tombstones. A complete
recovery of the recommended installer deployment must also preserve:

- `hsync-server-certs`, containing the certificate and matching encrypted key;
- the private `.env`, containing the API key and private-key password.

Losing either the certificate volume or its password changes the server TLS
identity and requires every client to establish trust again.

### Compose installer backup

Stop the canonical Compose deployment and archive both named volumes plus its
private configuration:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
BACKUP_DIR="$HOME/hsync-backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

docker compose -f "$INSTALL_DIR/compose.yaml" --project-directory "$INSTALL_DIR" stop
install -m 600 "$INSTALL_DIR/.env" "$BACKUP_DIR/hsync-config-$TIMESTAMP.env"
docker run --rm \
  -v hsync-server-buckets:/snapshot/buckets:ro \
  -v hsync-server-certs:/snapshot/certs:ro \
  -v "$BACKUP_DIR:/backup" \
  alpine:3.22 tar -czf "/backup/hsync-volumes-$TIMESTAMP.tar.gz" \
    -C /snapshot buckets certs
docker compose -f "$INSTALL_DIR/compose.yaml" --project-directory "$INSTALL_DIR" start
```

Store the archive and `.env` backup together in a location with access controls
appropriate for secrets.

### Compose installer restore

Restore only into a stopped server, and keep the current volume as a safety copy
until the restored data has been verified:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
ARCHIVE="$HOME/hsync-backups/hsync-volumes-20260101-120000.tar.gz"
CONFIG_BACKUP="$HOME/hsync-backups/hsync-config-20260101-120000.env"

docker compose -f "$INSTALL_DIR/compose.yaml" --project-directory "$INSTALL_DIR" stop
for volume in hsync-server-buckets-restored hsync-server-certs-restored; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Refusing to overlay existing restore volume: $volume" >&2
    exit 1
  fi
done
docker volume create hsync-server-buckets-restored
docker volume create hsync-server-certs-restored
docker run --rm \
  -v hsync-server-buckets-restored:/restore/buckets \
  -v hsync-server-certs-restored:/restore/certs \
  -v "$(dirname "$ARCHIVE"):/backup:ro" \
  alpine:3.22 tar -xzf "/backup/$(basename "$ARCHIVE")" -C /restore
install -m 600 "$CONFIG_BACKUP" "$INSTALL_DIR/.env"
```

After inspecting the restored volumes, change both volume `name` values in
`compose.yaml` to `hsync-server-buckets-restored` and
`hsync-server-certs-restored`, start the server, and run the verification below.
Keep the original volumes until the restore is confirmed.

### Backup strategies

**Method 1: Full backup with server stop (recommended for consistency)**

Best for scheduled backups when a brief downtime (~30 seconds) is acceptable.

```bash
#!/bin/bash
# backup-hsync.sh — run daily via cron
BACKUP_DIR="/var/backups/hsync"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RETENTION_DAYS=30
mkdir -p "$BACKUP_DIR"

./docker.sh stop
tar -czf "$BACKUP_DIR/hsync-buckets-$TIMESTAMP.tar.gz" -C buckets .
./docker.sh start --env-file prod.env

# Rotate old backups
find "$BACKUP_DIR" -name "hsync-buckets-*.tar.gz" -mtime +$RETENTION_DAYS -delete
```

**Method 2: Zero-downtime via filesystem snapshots (LVM/ZFS/btrfs)**

For high-availability deployments where stopping the server is not acceptable:

```bash
# ZFS example
zfs snapshot tank/hsync-buckets@backup-$(date +%Y%m%d)
zfs send tank/hsync-buckets@backup-$(date +%Y%m%d) | gzip > backup.zfs.gz

# LVM example
lvcreate -L 1G -s -n hsync-buckets-snap /dev/vg0/hsync-buckets
mount /dev/vg0/hsync-buckets-snap /mnt/snap
tar -czf backup.tar.gz -C /mnt/snap .
umount /mnt/snap
lvremove -f /dev/vg0/hsync-buckets-snap
```

**Method 3: Hot backup via `docker cp` (acceptable for small buckets)**

```bash
docker cp hsync-server:/project/buckets /tmp/hsync-buckets-backup
tar -czf "$BACKUP_DIR/hsync-buckets-$TIMESTAMP.tar.gz" -C /tmp hsync-buckets-backup .
rm -rf /tmp/hsync-buckets-backup
```

> **Warning**: `docker cp` on a live container is not atomic — files changed
> during the copy may be inconsistent. For buckets larger than a few GB, prefer
> Method 1 or 2.

### Restore

```bash
# 1. Stop the server
./docker.sh stop

# 2. (Optional) Rename current buckets for safety
mv buckets buckets.bak.$(date +%Y%m%d)

# 3. Restore from backup
mkdir -p buckets
tar -xzf "$BACKUP_DIR/hsync-buckets-20250101-120000.tar.gz" -C buckets

# 4. Start the server
./docker.sh start --env-file prod.env
```

### Restore verification

After restoring, verify the server sees the files correctly:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
SERVER_URL="https://127.0.0.1:9753"  # Use the configured LAN IP when applicable.

# Export and verify the restored public certificate.
docker compose -f "$INSTALL_DIR/compose.yaml" --project-directory "$INSTALL_DIR" \
  cp server:/project/certs/server.crt "$INSTALL_DIR/server.crt"
curl --cacert "$INSTALL_DIR/server.crt" "$SERVER_URL/health"

# List files in each bucket
HSYNC_API_KEY="$(grep '^HSYNC_API_KEY=' "$INSTALL_DIR/.env" | cut -d= -f2-)"
curl --cacert "$INSTALL_DIR/server.crt" \
  -H "Authorization: Bearer $HSYNC_API_KEY" \
  "$SERVER_URL/api/v1/buckets/mybucket/files"
```

### Disaster recovery scenario

If you lose the entire server (hardware failure, VM destroyed):

1. Provision a new server with Docker and Docker Compose v2 installed.
2. Download and verify the same or target release installer.
3. Run it with `--no-start` and the original bind address.
4. Restore the `.env`, bucket volume, and certificate volume as described above.
5. Start the Compose deployment and verify the certificate fingerprint and files.
6. Update DNS or the client server address if the host address changed. A changed
   address requires a certificate rotation and renewed client trust.

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

### Recommended Compose installer

Download and verify the installer for the target release, then run it with the
same installation directory. It preserves secrets and volumes, updates the
pinned image, and waits for the upgraded service to become healthy:

```bash
./install.sh --install-dir "$HOME/.local/share/hsync-server"
```

### Manual Docker Compose

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
curl http://localhost:9753/health
# → 200 OK (empty body)
```

Use this endpoint for container orchestration liveness probes and load balancer
health checks.

### Prometheus metrics

```bash
curl http://localhost:9753/metrics
```

The `/metrics` endpoint exposes standard FastAPI metrics (request count, latency,
response size) via `prometheus-fastapi-instrumentator`. Integrate with your
existing Prometheus + Grafana stack.

### Delta Transfer Metrics

Delta transfer operations expose the following Prometheus metrics:

- **`hsync_delta_transfer_total`**: Counter for delta transfer attempts
  - Labels: `direction` (upload/download), `status` (success/failure/rejected/conflict)
  - Use to track transfer success rates and identify failure patterns

- **`hsync_delta_bytes_saved`**: Counter for bytes saved by delta transfer
  - Label: `direction` (upload/download)
  - Use to measure bandwidth savings from delta transfer

- **`hsync_delta_transfer_duration_seconds`**: Histogram of transfer duration
  - Labels: `direction` (upload/download), `phase` (signatures/match/apply/data/total)
  - Buckets: 0.01s, 0.05s, 0.1s, 0.5s, 1s, 2s, 5s, 10s, 30s, 60s, 120s
  - Use to identify performance bottlenecks and slow transfers

- **`hsync_delta_block_change_ratio`**: Histogram of block change ratios
  - Label: `direction` (upload/download)
  - Buckets: 0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0
  - Use to identify when files are too different for delta to be worthwhile

- **`hsync_delta_active_tokens`**: Gauge for currently active download tokens
  - Use to monitor token usage and detect token leaks

- **`hsync_delta_temp_storage_bytes`**: Gauge for total bytes used by temp files
  - Use to monitor temp storage usage and detect storage leaks

### Example Prometheus Queries

```promql
# Delta transfer success rate (last 5 minutes)
rate(hsync_delta_transfer_total{status="success"}[5m]) / rate(hsync_delta_transfer_total[5m])

# Average transfer duration (last 5 minutes)
rate(hsync_delta_transfer_duration_seconds_sum[5m]) / rate(hsync_delta_transfer_duration_seconds_count[5m])

# Bytes saved per minute
rate(hsync_delta_bytes_saved[1m])

# Active tokens (current)
hsync_delta_active_tokens

# Temp storage usage (current)
hsync_delta_temp_storage_bytes
```

### Alerting Recommendations

Consider setting up alerts for:

- High failure rate: `rate(hsync_delta_transfer_total{status="failure"}[5m]) > 0.1`
- Long transfer duration: `histogram_quantile(0.95, rate(hsync_delta_transfer_duration_seconds_bucket[5m])) > 30`
- Token leak: `hsync_delta_active_tokens > 900` (approaching 1000 limit)
- Storage leak: `hsync_delta_temp_storage_bytes > 9 * 1024 * 1024 * 1024` (approaching 10GB limit)

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
- [Getting Started](getting-started.md) — recommended private-LAN install.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [FAQ](faq.md) — frequently asked questions and troubleshooting.
