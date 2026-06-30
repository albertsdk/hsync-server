# HSYNC Server — Documentation

The HSYNC Server is the central hub for Server Mode synchronization. It is a
FastAPI application that stores files in per-bucket directories, exposes a REST
API for listing/uploading/downloading/deleting files, and pushes change events
to connected clients over WebSocket.

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
`ghcr.io/albertobasso/hsync-server:v1.0.0`. Configuration is driven entirely by
`HSYNC_*` environment variables.

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
| [sync-flow.md](sync-flow.md) | End-to-end sync flow between clients and the server. |
| [faq.md](faq.md) | Frequently asked questions and why to choose HSYNC. |

For the ecosystem-wide decision records, see the
[ADRs in hsync-common](../../hsync-common/docs/adr/README.md).

## Related projects

- [hsync-client](../../hsync-client/docs/README.md) — the sync client / CLI.
- [hsync-desktop](../../hsync-desktop/docs/README.md) — desktop GUI front-end.
- [hsync-android](../../hsync-android/docs/README.md) — the Android app.
- [hsync-common](../../hsync-common/docs/README.md) — shared models and protocol.
