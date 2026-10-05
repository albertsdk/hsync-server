# HSYNC Server — LLM Wiki (Index)

Agent-optimized knowledge base for `hsync-server`, the FastAPI process that stores
bucket data, serves the REST file API, and relays real-time sync events over
WebSocket. It depends on `hsync-common` for models, the wire protocol, and
utilities.

> End-user / human docs live in [`../docs/`](../docs/README.md)
> (`getting-started`, `api.md`, `sync-flow.md`, `faq.md`,
> `internal/architecture.md`). This wiki is the **first search surface**:
> start here, follow the `path/to/file.py:line` references to reach code directly.

## Map

| Doc | Scope |
| --- | ----- |
| [architecture.md](architecture.md) | App bootstrap, middleware stack, routers, layering, request lifecycle |
| [modules/rest-api.md](modules/rest-api.md) | REST endpoints: buckets, files (upload/download/list/delete), tombstones, health |
| [modules/websocket.md](modules/websocket.md) | WS handshake, event handlers, connection manager, broadcasting |
| [modules/infrastructure.md](modules/infrastructure.md) | config/Settings, rate limiting, SSL, hashes, compression, errors, logging |
| [modules/benchmarks.md](modules/benchmarks.md) | L1 micro-benchmark suite (`bench/`): tiers, scenario→code anchors, isolation, methodology, findings |
| [data-models.md](data-models.md) | Server-local state (hash cache, tombstone store) + reuse of common models |
| [decisions.md](decisions.md) | Pointer to ecosystem ADRs under `../../hsync-common/docs/internal/adr/` |
| [deferred-design-issues.md](deferred-design-issues.md) | Analysed but deferred architectural issues (compression Content-Length) — read before implementing related features |

## Quick facts

- Package root: `hsync_server/` (`hsync_server/__init__.py:1`).
- ASGI app object: `app` in `hsync_server/main.py:77` (FastAPI, lifespan-managed).
- Version SSOT: `HSYNC_VERSION` re-exported from `hsync_common.constants`
  (`hsync_server/constants.py:25`); wire header names (`HEADER_*`) and the
  `X-Client-Name` validator (`is_valid_client_name`) are re-exported from the
  same SSOT (`:21`); `CONTENT_TYPE_OCTET_STREAM` at `:20`.
- API prefix: `/api/v1` hardcoded per route (e.g. `api/buckets.py:41`); admin
  sub-router prefix at `api/admin.py:54`.
- Protocol versions enforced from common SSOT (`hsync_common.protocol.handshake`):
  REST in `VersionMiddleware` (`middleware.py:130`), WS in `events.py` handshake.
- Auth: per-account tokens (`hs_<id>_<secret>`) verified via scrypt against the
  auth store (`auth.py` → `auth_store/`); roles (`admin`/`client`) + bucket
  membership gate access.
- Persistence: files under `get_buckets_path()`; causal versions/tombstones in
  per-bucket DBs `get_state_dir()/server/<bucket>.db` (ADR-0025). SHA-256 cache
  is in-memory.
- Standalone builds: `compile.sh` (targets `native`, `deb`, `deb-all`, `rpi`);
  the Raspberry Pi cross-build image is `packaging/rpi/Dockerfile.builder`, the
  Debian packaging tree is `packaging/deb/`. See
  [architecture.md](architecture.md#build--packaging).
- Distribution: `install.sh` (Docker Compose installer), `compose.yaml`,
  `Dockerfile`, `docker.sh` (source image build/lifecycle) and `run.sh`
  (uniform clone-and-run). The release publishes the GHCR image, the `.deb`
  packages and the Linux native binaries (see `decisions.md`). The four install
  methods are one page each under `docs/getting-started-<method>.md`.

## Conventions used in this wiki

- Every claim is anchored to code via `path/to/file.py:line`.
- No large code blocks are duplicated; intent is described and the code referenced.
- Kept in sync with `../docs/` per the Dual-Docs rule in `AGENTS.md`.
