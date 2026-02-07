# HSYNC Server — LLM Wiki (Index)

Agent-optimized knowledge base for `hsync-server`, the FastAPI process that stores
bucket data, serves the REST file API, and relays real-time sync events over
WebSocket. It depends on `hsync-common` for models, the wire protocol, and
utilities.

> End-user / human docs live in [`../docs/`](../docs/internal/architecture.md)
> (`architecture.md`, `api.md`, `sync-flow.md`, `faq.md`). This wiki is the **first
> search surface**: start here, follow the `path/to/file.py:line` references to
> reach code directly.

## Map

| Doc | Scope |
| --- | ----- |
| [architecture.md](architecture.md) | App bootstrap, middleware stack, routers, layering, request lifecycle |
| [modules/rest-api.md](modules/rest-api.md) | REST endpoints: buckets, files (upload/download/list/delete), tombstones, health |
| [modules/websocket.md](modules/websocket.md) | WS handshake, event handlers, connection manager, broadcasting |
| [modules/infrastructure.md](modules/infrastructure.md) | config/Settings, rate limiting, SSL, hashes, compression, errors, logging |
| [data-models.md](data-models.md) | Server-local state (hash cache, tombstone store) + reuse of common models |
| [decisions.md](decisions.md) | Pointer to ecosystem ADRs under `../../hsync-common/docs/internal/adr/` |
| [deferred-design-issues.md](deferred-design-issues.md) | Analysed but deferred architectural issues (compression Content-Length) — read before implementing related features |

## Quick facts

- Package root: `hsync_server/` (`hsync_server/__init__.py:1`).
- ASGI app object: `app` in `hsync_server/main.py:77` (FastAPI, lifespan-managed).
- Version SSOT: `HSYNC_VERSION = VERSION` (`hsync_server/constants.py:29`), sourced
  from `hsync_common.constants`.
- API prefix: `/api/v1` (`hsync_server/constants.py:38`).
- Protocol versions enforced from common SSOT (`hsync_common.protocol.handshake`):
  REST in `VersionMiddleware` (`middleware.py:111`), WS in `events.py` handshake.
- Auth: Bearer API key, constant-time compare (`routers.py:69`).
- Persistence: files under `get_buckets_path()`; causal versions/tombstones in
  `get_state_dir()/syncstate.db` (ADR-0025). SHA-256 cache is in-memory.

## Conventions used in this wiki

- Every claim is anchored to code via `path/to/file.py:line`.
- No large code blocks are duplicated; intent is described and the code referenced.
- Kept in sync with `../docs/` per the Dual-Docs rule in `CLAUDE.md`.
