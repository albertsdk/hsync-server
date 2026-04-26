# HSYNC Server — Architecture (agent view)

Single FastAPI process. REST for file transfer + bucket management; WebSocket for
real-time event fan-out. Bucket **bytes** are directories on disk; causal
versions, tombstones, and accounts live in SQLite `syncstate.db` (ADR-0025).

## Bootstrap (`main.py`)

`app = FastAPI(...)` (`main.py:77`) with a `lifespan` (`main.py:39`) that
bootstraps the admin account (via `auth_store.bootstrap_admin`) and preloads the
SHA-256 cache via `set_sha256_hashes` (`main.py:58` → `hashes.py:73`). Entry
points: `__main__.py:62` (`main` Typer CLI) and `__main__.py:268` (`run`);
console invocation goes through `__main__.py`.

## Middleware stack (outermost → innermost)

Added in `main.py` (order matters — last added runs first):

| Order | Middleware | Location | Role |
| ----- | ---------- | -------- | ---- |
| 1 | `VersionMiddleware` | `middleware.py:77` | hard-fail 426 on REST protocol mismatch; stamps `X-Server-Version`/`X-Protocol-Version` (`middleware.py:111`) |
| 2 | `RequestIdMiddleware` | `middleware.py:31` | per-request id for tracing (`logger.py:63` context) |
| 3 | `CORSMiddleware` (optional) | `main.py:107` | gated by `get_cors_enabled()` (`config.py:402`) |
| 4 | `GZipMiddleware` | `main.py:101` | compress responses > 1000 B |
| 5 | `SlowAPIMiddleware` | `main.py:88` | global rate limiting (`rate_limiter.py`) |

Prometheus metrics exposed at `/metrics` via `Instrumentator` (`main.py:125`).

## Routers (`routers.py`)

| Router | Auth | Location |
| ------ | ---- | -------- |
| `auth_router` | account token via `Depends(verify_api_key)` | `routers.py:75` |
| `admin_router` | account token + `admin` role | `api/admin.py:40` |
| `public_router` | none (health, metrics, time, WS) | `routers.py:83` |

`verify_api_key` (`auth.py`) extracts the Bearer token and verifies it against
the account store (`auth_store.py`): scrypt hash check with a KDF cache, then a
per-request DB re-check of status/generation/expiry. Failures raise
`MissingApiKeyError`/`InvalidApiKeyError`; authorization failures raise
`ForbiddenError`/`ConflictError` (`exceptions.py`). Bucket-scoped endpoints
enforce membership via `check_bucket_access` (`api/authz.py`). Endpoints attach
to a router by decorator in their own modules (buckets, files, events, admin,
members, health), so `main.py` only imports the modules and includes the routers.

## Layering

```
config / constants / exceptions / logger            (leaf)
        │
hashes / compression / ssl_utils / rate_limiter / path_validation
        │
auth_store / auth / error_handlers / middleware / routers   (app plumbing)
        │
api/  (buckets, files/*, events + events_handlers/*, admin, members, authz, health)
        │
main.py  (assembles app, lifespan, middleware, routers, metrics)
```

Higher layers import lower ones. `api/*` reuses `hsync_common.models` and
`hsync_common.protocol.*` rather than redefining wire types.

## Request lifecycles

| Flow | Path | Anchor |
| ---- | ---- | ------ |
| Upload | `POST /api/v1/buckets/{bucket}/files` | `api/files/upload.py:70` |
| Download | `GET /api/v1/buckets/{bucket}/files/download` | `api/files/download.py:43` |
| List | `GET /api/v1/buckets/{bucket}/files` | `api/files/list.py:124` |
| Delete | `DELETE /api/v1/buckets/{bucket}/files` | `api/files/delete.py:44` |
| WS events | `WS /api/v1/buckets/{bucket}/events/{cid}` | `api/events.py:62` |

REST upload writes to disk, updates the hash cache, records/clears tombstones, and
broadcasts the resulting event to WS subscribers of the bucket via `bucket_manager`
(`connection_manager.py:166`). See [modules/rest-api.md](modules/rest-api.md) and
[modules/websocket.md](modules/websocket.md).

## Error handling

`register_exception_handlers` (`error_handlers.py:166`) maps `HSyncError`
subclasses to HTTP codes through `EXCEPTION_STATUS_CODES` (`error_handlers.py:53`),
plus a request-validation handler (`:112`) and a catch-all (`:140`). Responses use
the `ErrorResponse` model (`error_handlers.py:37`).

## Storage model

Buckets live under `get_buckets_path()` (`config.py:362`), one directory per
bucket. Empty dirs are kept alive by a sentinel file (`api/file_utils.py:29`),
partial uploads use the `.part` suffix (common `constants.py`), and deletions leave
tombstones (`tombstones.py`). Causal versions/tombstones live in
`get_state_dir()/syncstate.db` (ADR-0025); accounts and bucket membership live in
the same file via the server-only `clients`/`bucket_members` tables
(`auth_store.py`). The SHA-256 cache is an in-memory unbounded dict, rebuilt at startup (not LRU).

> Human-oriented diagrams (system overview, upload/WS sequences):
> `../docs/internal/architecture.md`.
> Decision rationale: [decisions.md](decisions.md) →
> `../../hsync-common/docs/internal/adr/`.
