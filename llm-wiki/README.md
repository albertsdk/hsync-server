# HSYNC Server — LLM Wiki (Index)

Agent-optimized knowledge base for `hsync-server`, the FastAPI process that stores
bucket data, serves the REST file API, and relays real-time sync events over
WebSocket. It depends on `hsync-common` for models, the wire protocol, and
utilities.

> End-user / human docs live in [`../docs/`](../docs/README.md)
> (`getting-started`, `api.md`, `sync-flow.md`, `faq.md`,
> `internal/architecture.md`). This wiki is the **first search surface**:
> start here, follow the `path::symbol` references to reach code directly.

**Source references.** Local paths are relative to this repository root and use
`path/to/file.ext::Symbol`. Cross-repo references are relative to the `hsync/`
workspace root and start with the sibling repo directory (`hsync-common/…`,
`hsync-client/…`, `hsync-server/…`, `hsync-rendezvous/…`, `hsync-desktop/…`);
they are code spans, not hyperlinks.

## Map

| Doc | Scope |
| --- | ----- |
| [architecture.md](architecture.md) | App rendezvous, middleware stack, routers, layering, request lifecycle |
| [modules/rest-api.md](modules/rest-api.md) | REST endpoints: buckets, files (upload/download/list/delete), tombstones, health |
| [modules/websocket.md](modules/websocket.md) | WS handshake, event handlers, connection manager, broadcasting |
| [modules/infrastructure.md](modules/infrastructure.md) | config/Settings, rate limiting, SSL, hashes, compression, errors, logging |
| [modules/benchmarks.md](modules/benchmarks.md) | L1 micro-benchmark suite (`bench/`): tiers, scenario→code anchors, isolation, methodology, findings |
| [data-models.md](data-models.md) | Server-local state (hash cache, tombstone store) + reuse of common models |
| [decisions.md](decisions.md) | Pointer to ecosystem ADRs under `../../hsync-common/docs/internal/adr/` |
| [deferred-design-issues.md](deferred-design-issues.md) | Analysed but deferred architectural issues (compression Content-Length) — read before implementing related features |

## Quick facts

- Package root: `hsync_server/`.
- ASGI app object: `app` (`hsync_server/main.py::app`) (FastAPI, lifespan-managed).
- Version SSOT: `HSYNC_VERSION` re-exported from `hsync_common.constants`
  (`hsync-common/hsync_common/constants.py::HSYNC_VERSION`); wire header names (`HEADER_*`) and
  the `X-Client-Name` validator (`is_valid_client_name`) are re-exported from the
  same SSOT (`hsync-common/hsync_common/constants.py::HEADER_CLIENT_NAME`,
  `hsync-common/hsync_common/constants.py::is_valid_client_name`); `CONTENT_TYPE_OCTET_STREAM`
  at `hsync-common/hsync_common/constants.py::CONTENT_TYPE_OCTET_STREAM`.
- API prefix: `/api/v1` from the shared SSOT
  (`hsync-common/hsync_common/protocol/rest.py::REST_API_PREFIX`), applied per route via the
  `rest_*` helpers (e.g. `hsync_server/api/buckets.py::get_bucket`); admin sub-router at
  `hsync_server/api/admin.py::admin_router`.
- Protocol versions enforced from common SSOT (`hsync_common.protocol.handshake`):
  REST in `VersionMiddleware` (`hsync_server/middleware.py::VersionMiddleware`), WS in
  `api/events.py` handshake.
- Auth: per-account tokens (`hs_<id>_<secret>`) verified via scrypt against the
  auth store (`auth.py` → `auth_store/`); roles (`admin`/`client`) + bucket
  membership gate access.
- Persistence: files under `get_buckets_path()` (`hsync_server/config.py::get_buckets_path`);
  causal versions/tombstones in per-bucket DBs `get_state_dir()/server/<bucket>.db`
  (ADR-0025, `hsync_server/config.py::get_state_dir`). SHA-256 cache is in-memory.
- Packaged installs: the frozen binary reads `/etc/hsync-server/hsync-server.conf`
  itself (`hsync_server/__main__.py::_load_system_env_file`, path SSOT
  `hsync_server/constants.py::SYSTEM_CONFIG_FILE`) and defaults its storage roots
  to the CWD (`hsync_server/config.py::_STORAGE_ROOT`); a missing buckets root
  aborts the CLI before uvicorn starts (`hsync_server/__main__.py::main`).
- Standalone builds: `compile.sh` (targets `native`, `deb`, `deb-all`, `rpm`,
  `rpm-all`, `rpi`);
  the Raspberry Pi cross-build image is `packaging/rpi/Dockerfile.builder`, the
  Debian packaging tree is `packaging/deb/`. See
  [architecture.md](architecture.md#build--packaging).
- Distribution: `install.sh` (Docker Compose installer: verifies the image's
  Sigstore signature before writing any file), `compose.yaml`,
  `Dockerfile`, `docker.sh` (source image build/lifecycle) and `run.sh`
  (uniform clone-and-run). The release publishes the GHCR image, the `.deb` and
  `.rpm` packages and the Linux native binaries (see `decisions.md`). The five
  install methods are one page each under `docs/getting-started-<method>.md`.

## Conventions used in this wiki

- Every claim is anchored to code via `path::symbol` (never line numbers).
- No large code blocks are duplicated; intent is described and the code referenced.
- Kept in sync with `../docs/` per the Dual-Docs rule in `AGENTS.md`.
