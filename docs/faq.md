# Frequently Asked Questions (Server)

Common questions about running the HSYNC server. For deeper detail each answer
links to the relevant guide.

## General

### Why run an HSYNC server instead of another sync backend?

- **One container, no database.** The server is a single FastAPI app that stores
  files as plain files in per-bucket directories — there is no database to
  provision, tune, or back up separately. See [Architecture](architecture.md).
- **Run it anywhere.** Pull the prebuilt multi-arch image (`amd64`/`arm64`) from
  GHCR, build locally with `./docker.sh`, or run straight from Poetry. See
  [README → Install & quickstart](README.md#install--quickstart).
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
  clients.

### Do I need to run a server at all?

Only for remote / cross-network sync. Devices on the same network can use the
client's serverless **LAN Mode** instead. See
[client LAN Mode](../../hsync-client/docs/lan-mode.md).

## Deployment & Configuration

### How do I configure the server?

Entirely through `HSYNC_*` environment variables (host/port, API key, SSL, rate
limits). See [README → Install & quickstart](README.md#install--quickstart).

### What port does the server use?

`8765` by default. Map or override it as needed when you start the container. See
[README → Install & quickstart](README.md#install--quickstart).

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

The server is designed to run over HTTPS/WSS — either with its own SSL settings or
behind a TLS-terminating reverse proxy. See [Architecture](architecture.md).

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
[README → Install & quickstart](README.md#install--quickstart).

## See also

- [README](README.md) — install and quickstart.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [Sync Flow](sync-flow.md) — end-to-end synchronization behavior.
