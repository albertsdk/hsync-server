# Frequently Asked Questions (Server)

Common questions about running the HSYNC server. For deeper detail each answer
links to the relevant guide.

## General

### Why run an HSYNC server instead of another sync backend?

- **One container, no database.** The server is a single FastAPI app that stores
  files as plain files in per-bucket directories — there is no database to
  provision, tune, or back up separately. See [Architecture](architecture.md).
- **Run it anywhere.** Pull the prebuilt multi-arch image (`amd64`/`arm64`) from
  GHCR with the [recommended installer](getting-started.md), or choose a source
  or native method from [Installation Alternatives](installation-alternatives.md).
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
client's serverless **LAN Mode** instead. See
[client LAN Mode](https://github.com/albertobasso/hsync-client/blob/main/docs/lan-mode.md).

## Deployment & Configuration

### How do I configure the server?

Entirely through `HSYNC_*` environment variables. See the
[Environment Variables table](README.md#environment-variables) in the README
for the complete list with defaults and descriptions.

### What port does the server use?

`9753` by default. Map or override it as needed when you start the container. See
[Getting Started](getting-started.md).

### Can I put the server behind a reverse proxy?

Yes. Terminate TLS at the proxy and forward to the server; clients connect to the
proxy's `https://` URL. See [Architecture](architecture.md).

### Where are my files stored?

Under the buckets directory (`/project/buckets` in the container), one
sub-directory per bucket, as ordinary files — so backing up is just copying that
directory. See [Architecture](architecture.md).

## Security

### How is access controlled?

Authenticated endpoints require an `Authorization: Bearer <HSYNC_API_KEY>` token,
compared in constant time. Set the key via the `HSYNC_API_KEY` environment
variable. See [API → Authentication](api.md#authentication-flow).

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
send API keys over plain HTTP. See [Architecture](architecture.md) and
[Security Best Practices → TLS Configuration](security-best-practices.md#tls-configuration).

### How does the server protect against abuse?

Endpoints enforce per-IP rate limits (configurable via environment variables). See
[API → REST API Endpoints](api.md#rest-api-endpoints).

## Sync Behavior

### What happens when two clients change the same file?

The server stores the latest upload and broadcasts the event; clients reconcile
according to their sync direction and mode. See [Sync Flow](sync-flow.md).

### How are deletions handled across offline clients?

Through **tombstones**: deletion markers (merged last-write-wins) let peers that
were offline at deletion time avoid resurrecting files during a full
reconciliation. See [API → Tombstones](api.md#tombstones).

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

Create, inspect, and delete buckets through the REST API. See
[API → Buckets](api.md#buckets).

### How do I pin a specific server version?

Use a version tag instead of `latest`, e.g.
`ghcr.io/albertobasso/hsync-server:v1.0.0`. See
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
- `HSYNC_API_KEY` not set — the server requires an API key.
- `HSYNC_SSL_KEYFILE_PASSWORD` missing when `HSYNC_SSL_ENABLED=true`.
- Volume permissions — the container user (UID 1000) must be able to write to
  the buckets and certs directories.

**"HSYNC_SSL_KEYFILE_PASSWORD is required when SSL is enabled":**
Set the `HSYNC_SSL_KEYFILE_PASSWORD` environment variable. This is mandatory
whenever `HSYNC_SSL_ENABLED=true` because the server stores the private key
encrypted on disk.

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
  [Client Configuration → SSL/TLS](https://github.com/albertobasso/hsync-client/blob/main/docs/configuration.md#ssltls-httpswss)
  guide.
- Clients always connect with `https://` / `wss://` and refuse to send API keys
  over plain HTTP.

**"401 Unauthorized" or "403 Forbidden":**
- Verify the client is sending the correct `Authorization: Bearer <key>` header.
- Check that `HSYNC_API_KEY` on the server matches the client's key.
- The server compares API keys in constant time to prevent timing attacks.

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
- A file with the same SHA-256 hash already exists on the server.
- This is normal — the client can skip the upload.

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
- The SHA-256 cache is in-memory and grows with the number of unique files
  uploaded since the last restart. It is automatically evicted (LRU).

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
| 409 | Conflict | File already exists (same SHA-256) |
| 422 | Unprocessable Entity | Request body validation failed |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Unexpected server error |

For a complete list of error codes, see [API → Error Codes Reference](api.md#error-codes-reference).

## See also

- [README](README.md) — install and quickstart.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [Sync Flow](sync-flow.md) — end-to-end synchronization behavior.
- [Deployment Guide](deployment.md) — production deployment, backup, and monitoring.
