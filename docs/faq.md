# Frequently Asked Questions (Server)

Common questions about running the HSYNC server. For deeper detail each answer
links to the relevant guide.

## General

### Why run an HSYNC server instead of another sync backend?

- **One container, files on disk.** The server is a single FastAPI app that
  stores synced bytes as plain files in per-bucket directories. Causal
  versions, tombstones, and accounts live in SQLite (per-bucket DBs and `auth.db` in the
  state directory) — back that file up with the buckets. See
  [Architecture](internal/architecture.md).
- **Run it anywhere.** Pull the prebuilt multi-arch image (`amd64`/`arm64`) from
  GHCR with the recommended Docker installer ([Path A](getting-started-docker.md)),
  or choose a source or native method from
  [Installation Alternatives](installation-alternatives.md).
- **Secure by default.** Every authenticated endpoint requires a Bearer API key
  (constant-time comparison) and is meant to run behind HTTPS/WSS.
- **Real-time fan-out.** A WebSocket hub relays change events to all connected
  clients the moment a file is uploaded, so peers stay in sync instantly. See
  [Sync Flow](sync-flow.md).
- **Resilient transfers.** Uploads are resumable (offset tracking) and protected
  by per-IP rate limits, so flaky networks and large files are handled gracefully.
  See [API → Files](api.md#files).
- **Deletion-safe.** Tombstones (deletion markers) stop offline peers from
  resurrecting files they missed being deleted. See
  [API → Tombstones](api.md#tombstones).
- **Open ecosystem.** It is the shared hub for the CLI, desktop, and Android
  clients. The desktop GUI and Android app are still under development; the CLI
  is the production-ready client today.

### Do I need to run a server at all?

Only for remote / cross-network sync. Devices on the same network can use the
client's serverless **p2p Mode** instead. See
[client p2p Mode](https://github.com/albertsdk/hsync-client/blob/main/docs/p2p-mode.md).

## Deployment & Configuration

### How do I configure the server?

Entirely through `HSYNC_*` environment variables. See the
[Environment Variables table](README.md#environment-variables) in the README
for the complete list with defaults and descriptions.

### What port does the server use?

`9753` by default. Map or override it as needed when you start the server. See
the [Getting Started paths](getting-started.md).

### Can I put the server behind a reverse proxy?

Yes. Terminate TLS at the proxy and forward to the server; clients connect to the
proxy's `https://` URL. See [Architecture](internal/architecture.md).

### Where are my files stored?

User files live under the buckets directory (`/project/buckets` in the
container), one sub-directory per bucket. Causal versions and deletion
markers live in `HSYNC_STATE_DIR` (`/project/state/server/`). Back up
both. See [Architecture](internal/architecture.md).

When clients sync with `--ignore-delete` (pure backup mode), the bucket
accumulates append-only history: files removed on the client stay on the
server, and copying the buckets directory captures all of it — no special
server configuration is needed. See the
[client backup examples](https://github.com/albertsdk/hsync-client/blob/main/docs/examples-server.md#13-push-only-backup-to-a-remote-server).

## Security

### How is access controlled?

Authenticated endpoints require an `Authorization: Bearer hs_<id>_<secret>`
account token. Each account has a role (`admin` or `client`) and bucket
memberships; a `client` account can only access buckets it owns or is a member
of. See [Roles and Permissions](roles-and-permissions.md) and
[API → Authentication](api.md#authentication).

### Is communication encrypted?

Yes. Two transport modes are supported:

- **Reverse proxy (recommended for Internet-facing servers)**: run the server
  behind a TLS-terminating reverse proxy (nginx, Caddy, Traefik). The proxy
  serves `https://`/`wss://` externally and forwards to the server over the
  internal network; the built-in SSL terminator stays disabled
  (`HSYNC_SSL_ENABLED=false`).
- **Built-in TLS with a persistent self-signed certificate (trusted private
  LAN)**: enable `HSYNC_SSL_ENABLED` and the server terminates HTTPS/WSS itself.
  The self-signed certificate is persisted so its fingerprint stays stable
  across restarts, which is what makes it usable for a trusted LAN — it is not
  meant for exposing the server directly to the Internet.

In both cases clients always connect with `https://` / `wss://` and refuse to
send API keys over plain HTTP. See [Architecture](internal/architecture.md) and
[Security Best Practices → TLS Configuration](security-best-practices.md#tls-configuration).

### How does the server protect against abuse?

Endpoints enforce per-IP rate limits (configurable via environment variables). See
[API → REST API Endpoints](api.md#rest-api-endpoints).

## Sync Behavior

### What happens when two clients change the same file?

Every upload is arbitrated causally against the stored version (ADR-0022): the
server accepts the causally-dominant content, preserves the loser of a true
concurrent divergence as a `_conflicted_copy_` sibling, and rejects stale
ancestors. It then broadcasts the event; clients reconcile according to their
sync direction and mode. See [Sync Flow](sync-flow.md).

### How are deletions handled across offline clients?

Through **tombstones**: deletion markers, merged causally (the dominant vector
clock wins, never the deletion timestamp), let peers that were offline at
deletion time avoid resurrecting files during a full reconciliation. See
[API → Tombstones](api.md#tombstones).

### Does the server protect against mass deletion?

No — the mass-deletion guard lives in the **client**, not the server. The
server faithfully applies every deletion requested by an authenticated
client: a client started with `--no-mass-delete-guard`, or anyone holding
the API key, can empty a bucket without any server-side obstacle.

The server-side defences are:

- **API-key hygiene** — the key grants full read/write authority over every
  bucket; treat it accordingly and rotate it if it may have leaked.
- **Periodic backups / filesystem snapshots of the data directory** — the last
  line of defence against a propagated mass deletion, letting you roll the
  bucket back to a
  known-good state. See
  [Operations → Backup and Restore](operations.md#backup-and-restore).

See the
[client mass-deletion safety guard](https://github.com/albertsdk/hsync-client/blob/main/docs/configuration.md#mass-deletion-guard)
for the client-side protection.

### What happens if a client disconnects mid-upload?

Uploads are resumable: a client can query the current offset and continue rather
than restart. See
[API → Files](api.md#files) and [Sync Flow](sync-flow.md).

### How do clients keep clocks aligned?

The server exposes a timestamp endpoint (`/api/v1/time`) clients use to correct
clock skew. See [API → Monitoring & Sync](api.md#monitoring--sync).

## Operations

### How do I monitor the server?

`GET /health` returns an empty `200 OK` for liveness probes and `GET /metrics`
exposes Prometheus metrics (both unauthenticated). See
[API → Monitoring & Sync](api.md#monitoring--sync).

### How do I manage buckets?

Create, inspect, and delete buckets either through the REST API
([API → Buckets](api.md#buckets)) or through the local admin CLI
(`hsync-server admin bucket create/list/delete …` — see
[Roles and Permissions → CLI Tools](roles-and-permissions.md#cli-tools)
for the full syntax and per-installation invocation).

### How do I pin a specific server version?

Use a version tag instead of `latest`, e.g.
`ghcr.io/albertsdk/hsync-server:v1.0.0`. See
[Getting Started](getting-started.md).

## Troubleshooting

### Server won't start

**"Address already in use" on port 9753:**
```bash
# Find the process using the port
sudo lsof -i :9753
# Or use ss
sudo ss -tlnp | grep 9753
```
Either stop the conflicting process or change the server port with `HSYNC_PORT`.

**Container exits immediately:**
```bash
# Check the container logs
docker logs hsync-server
```
Common causes:
- `HSYNC_ADMIN_KEY` malformed — must be exactly 64 lowercase hex characters
  (generate with `openssl rand -hex 32`). If unset, the server generates and
  prints a one-time admin token instead.
- Data directory permissions — the container runs as the configured user
  (`HSYNC_UID`/`HSYNC_GID`, defaulting to the invoking user) and must be able
  to write to the bind-mounted data directory.

### Clients cannot connect

**Connection refused:**
```bash
# Verify the server is listening
curl http://localhost:9753/health

# Check firewall rules
sudo ufw status
# Open the port if needed
sudo ufw allow 9753/tcp
```

**TLS/SSL errors:**
- If using a reverse proxy, ensure the proxy is running and the certificate is
  valid.
- If using self-signed certificates, clients need `--ca-cert <file>` or
  `--insecure`. See the
  [Client Configuration — Server Mode → Server Connection Options](https://github.com/albertsdk/hsync-client/blob/main/docs/configuration-server.md#server-connection-options)
  guide.
- Clients always connect with `https://` / `wss://` and refuse to send API keys
  over plain HTTP.

**"401 Unauthorized" or "403 Forbidden":**
- Verify the client is sending the correct `Authorization: Bearer <token>` header.
- Check that the account token is valid, active, and not expired. A `403` means
  the account is authenticated but lacks permission (e.g. a `client` account on
  an `/admin` endpoint, or a member on an owner-only operation).
- The server compares token hashes in constant time to prevent timing attacks.

**WebSocket connection fails:**
- Ensure the client sends an `auth` frame as the first message after connecting.
- The WebSocket URL format is:
  ```
  wss://host:9753/api/v1/buckets/{bucket}/events/{cid}
  ```
- Check the WebSocket close code:
  - `4001` — first frame was not a valid `auth` message.
  - `4002` — protocol version mismatch.
  - `4003` — invalid API key.
  - `4008` — authentication timeout (no auth frame within the timeout window).
- See [API → WebSocket Authentication Flow](api.md#authentication-flow) for details.

**Rate limited (429 Too Many Requests):**
- The client IP may have exceeded the per-IP rate limit.
- If behind a reverse proxy, ensure `HSYNC_TRUSTED_PROXIES` includes the proxy's
  IP range so that `X-Forwarded-For` is honoured.
- Check the current rate limits in the
  [Environment Variables table](README.md#environment-variables) of the README.

### File operations fail

**Upload fails with "File too large":**
- The file exceeds `HSYNC_MAX_UPLOAD_SIZE` (default 10 GiB).
- Increase the limit or split the file.

**Upload fails with "Insufficient disk space":**
- The server checks `HSYNC_MIN_FREE_DISK_SPACE` before accepting uploads.
- Free up disk space or set `HSYNC_MIN_FREE_DISK_SPACE=0` to disable the check.

**Upload returns 409 Conflict:**
- A file with the same SHA-256 hash already exists on the server (duplicate
  upload; the client can skip it), or the upload lost the causal arbitration: it
  was superseded by a newer version, or it lost the deterministic tie-break
  (its content is preserved as a `_conflicted_copy_`).
- These are normal outcomes — the client handles each one automatically.

**Download fails with 404:**
- The file does not exist in the specified bucket.
- Verify the bucket name and file path.

**"Path traversal" error:**
- The server validates that file paths do not escape the bucket directory.
- Ensure the file path does not contain `..` or start with `/`.

### Performance issues

**Slow file listings:**
- Buckets with very large numbers of files (>100k) may experience slow `readdir`
  performance. Organise files into subdirectories.

**High memory usage:**
- Each WebSocket connection uses approximately 50–100 KB of RAM.
- Reduce `HSYNC_WS_MAX_CONNECTIONS` if memory is constrained.
- The SHA-256 cache is an in-memory dict rebuilt at startup. It is **not**
  LRU-capped: entries are dropped only when the file's mtime/size no longer
  match. Memory grows with the number of hashed paths for the process lifetime.

**Slow uploads/downloads:**
- Check network bandwidth and latency.
- Compression is enabled by default for downloads (zstd). Files under 10 KB and
  already-compressed formats are skipped.
- Uploads are resumable — if a transfer is interrupted, the client can resume
  from the last offset.

### Where to find server logs

| Deployment | Log location |
|------------|-------------|
| Docker (no `HSYNC_LOG_FILE`) | `docker logs hsync-server` |
| Docker (with `HSYNC_LOG_FILE`) | Inside the container at the configured path, or `docker logs` |
| Docker Compose | `docker compose logs hsync-server` |
| Systemd | `journalctl -u hsync-server` |
| Direct (Poetry) | stdout/stderr, or the configured log file |

Enable debug logging for more verbose output:

```bash
HSYNC_LOG_LEVEL=debug ./docker.sh start
```

### Common error codes

| HTTP Status | Meaning | Common causes |
|-------------|---------|---------------|
| 400 | Bad Request | Invalid parameters, malformed request |
| 401 | Unauthorized | Invalid API key |
| 403 | Forbidden | Missing API key |
| 404 | Not Found | Bucket or file does not exist |
| 409 | Conflict | Duplicate content, or upload superseded / lost the causal tie-break |
| 422 | Unprocessable Entity | Request body validation failed |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Unexpected server error |
| 507 | Insufficient Storage | Not enough free disk space for upload |

For a complete list of error codes, see [API → Error Codes Reference](api.md#error-codes-reference).

## See also

- [README](README.md) — install and quickstart.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [Sync Flow](sync-flow.md) — end-to-end synchronization behavior.
- [Deployment Guide](deployment.md) — production deployment, backup, and monitoring.
