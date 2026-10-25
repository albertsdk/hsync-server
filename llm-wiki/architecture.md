# HSYNC Server — Architecture (agent view)

Single FastAPI process. REST for file transfer + bucket management; WebSocket for
real-time event fan-out. Bucket **bytes** are directories on disk; causal
versions, tombstones, and accounts live in SQLite: one DB per bucket
(`server/<bucket>.db`) plus a global `auth.db` (ADR-0025).

## Bootstrap (`main.py`)

`app = FastAPI(...)` (`main.py::app`) with a `lifespan` (`main.py::lifespan`) that
bootstraps the admin account (via `auth_store.bootstrap_admin`) and preloads the
SHA-256 cache via `set_sha256_hashes` (`main.py::lifespan` → `hashes.py::set_sha256_hashes`).
Without `HSYNC_ADMIN_KEY`, `auth_store/accounts.py::AuthStore.bootstrap_admin`
delivers the generated token on stderr only for a TTY; otherwise it writes
`<state_dir>/bootstrap_admin_token` (mode 0600), so it never reaches a captured
log stream. Entry points: `main` Typer CLI (`__main__.py::main`) and `run`
(`__main__.py::run`); console invocation goes through `__main__.py`.

## Middleware stack (outermost → innermost)

Added in `main.py` (order matters — last added runs first):

| Order | Middleware | Location | Role |
| ----- | ---------- | -------- | ---- |
| 1 | `VersionMiddleware` | `middleware.py::VersionMiddleware` | hard-fail 426 on REST protocol mismatch; rejects a malformed `X-Client-Name` with 400 (shared `is_valid_client_name` SSOT); stamps `X-Server-Version`/`X-Protocol-Version` |
| 2 | `RequestIdMiddleware` | `middleware.py::RequestIdMiddleware` | per-request id for tracing (`logger.py::get_request_id` context) |
| 3 | `CORSMiddleware` (optional) | `main.py::configure_cors` | only with a non-empty explicit origin list (`config.py::get_cors_origins`); never wildcard + credentials (`allow_credentials=False`) |
| 4 | `TransferAwareGZipMiddleware` | `middleware.py::TransferAwareGZipMiddleware` | gzip on responses > 1000 B, **except** the transfer payload paths (`…/files/download`, `…/files/delta/data/{token}`), whose coding is the sync protocol's own (identity \| zstd) and must reach clients byte-identical |
| 5 | `SlowAPIMiddleware` | `main.py::app` | global rate limiting (`rate_limiter.py`) |

Prometheus metrics exposed at `/metrics` via `Instrumentator` (`main.py::app`).

## Routers (`routers.py`)

| Router | Auth | Location |
| ------ | ---- | -------- |
| `auth_router` | account token via `Depends(verify_api_key)` | `routers.py::auth_router` |
| `admin_router` | account token + `admin` role | `api/admin.py::admin_router` |
| `public_router` | none (health, metrics, time, WS) | `routers.py::public_router` |

`verify_api_key` (`auth.py`) extracts the Bearer token and verifies it against
the account store (`auth_store/`): scrypt hash check with a KDF cache, then a
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
| Upload | `POST /api/v1/buckets/{bucket}/files` | `api/files/upload.py::upload_file` |
| Download | `GET /api/v1/buckets/{bucket}/files/download` | `api/files/download.py::download_file` |
| List | `GET /api/v1/buckets/{bucket}/files` | `api/files/list.py::get_files` |
| Delete | `DELETE /api/v1/buckets/{bucket}/files` | `api/files/delete.py::delete_file` |
| WS events | `WS /api/v1/buckets/{bucket}/events/{cid}` | `api/events.py::ws_events` |

REST upload writes to disk, updates the hash cache, records/clears tombstones, and
broadcasts the resulting event to WS subscribers of the bucket via `bucket_manager`
(`api/connection_manager.py::bucket_manager`). See [modules/rest-api.md](modules/rest-api.md) and
[modules/websocket.md](modules/websocket.md).

## Error handling

`register_exception_handlers` (`error_handlers.py::register_exception_handlers`) maps `HSyncError`
subclasses to HTTP codes through `EXCEPTION_STATUS_CODES` (`error_handlers.py::EXCEPTION_STATUS_CODES`),
plus a request-validation handler (`error_handlers.py::validation_exception_handler`)
and a catch-all (`error_handlers.py::global_exception_handler`). Handlers emit a
uniform JSON error envelope (`error_handlers.py::hsync_exception_handler`).

## Storage model

Buckets live under `get_buckets_path()` (`config.py::get_buckets_path`), one directory per
bucket. Empty dirs are kept alive by a sentinel file (`api/file_utils.py::ensure_sentinel_in_dir`),
partial uploads use the `.part` suffix (common `constants.py`), and deletions leave
tombstones (`tombstones.py`). Causal versions/tombstones live in per-bucket DBs
`get_state_dir()/server/<bucket>.db` (ADR-0025); accounts and bucket membership
live in the global `server/auth.db` via the server-only `clients`/`bucket_members` tables
(`auth_store/`). The SHA-256 cache is an in-memory bounded LRU, rebuilt at startup.

## Build & packaging

`compile.sh` freezes the server into a standalone executable with PyInstaller.
Six targets:

| Target | Output | Toolchain |
| ------ | ------ | --------- |
| `native` (default) | `dist/<os>-<arch>/hsync-server` | host `.venv` + Poetry `build` group (`pyinstaller`) |
| `deb` | `dist/deb/hsync-server_<ver>_<arch>.deb` | host `.venv` + `dpkg-deb`, onedir freeze staged under `/usr/lib/hsync-server` |
| `deb-all` | `dist/deb/hsync-server_<ver>_{amd64,arm64,armhf}.deb` | Docker cross-build from `packaging/deb/Dockerfile.builder` under `qemu-user` binfmt |
| `rpm` | `dist/rpm/hsync-server-<ver>-1.<dist>.<arch>.rpm` | host `.venv` + `rpmbuild`, same onedir payload staged for `/usr/lib/hsync-server` |
| `rpm-all` | `dist/rpm/hsync-server-<ver>-1.el9.{x86_64,aarch64}.rpm` | Docker cross-build from `packaging/rpm/Dockerfile.builder` (Rocky Linux 9) under `qemu-user` binfmt |
| `rpi` | `dist/rpi/{armhf,arm64}/hsync-server` | Docker cross-build from `packaging/rpi/Dockerfile.builder` (Debian bookworm, Python 3.14.9, Poetry, Rust) under `qemu-user` binfmt |

The `native`/`rpi` targets share `_onefile_pyi_flags` (`compile.sh`), a onefile
flag set that collects the `hsync_server`/`hsync_common` submodules and pins the
uvicorn and websockets dynamic import points the ASGI app needs at runtime. The
`deb`/`deb-all` targets share `_onedir_pyi_flags` (`--onedir` variant) and stage
`packaging/deb/` (DEBIAN control + maintainer scripts, `hsync-server.service`,
`/etc/hsync-server/hsync-server.conf` conffile, man page) with
`_finalize_deb_stage` (`compile.sh`). `_semver_to_debian` maps a SemVer
prerelease to the Debian form (`-`→`~`), `_arch_to_deb` maps `uname -m`, and
`_inject_shlib_depends` runs `dpkg-shlibdeps` so `libc6`/`zlib1g` land in
`Depends`. The package installs under `/usr/lib/hsync-server` with a
`/usr/bin/hsync-server` symlink and a dedicated `_hsync-server` user, and its
unit is registered but not enabled. The `rpm`/`rpm-all` targets reuse the same
onedir payload and `packaging/rpm/` (`.spec`, Rocky Linux 9 builder);
`_semver_to_rpm`/`_arch_to_rpm` mirror the deb helpers, the man page and
conffile are packaged from the deb payload, and an RPM erase keeps the data and
the `_hsync-server` user (there is no `purge` equivalent). `ENTRY_POINT` is
`hsync_server/__main__.py`; the console entry point is `run()`
(`__main__.py::run`). The frozen app exposes the same CLI as
`python -m hsync_server`.

`deb-all`, `rpm-all` and `rpi` need Docker with `qemu-user-static` binfmt
handlers; `deb`/`rpm` need only `dpkg-deb`/`rpmbuild`, and `native` does not
touch Docker. The server ships as the multi-arch container image **and** as
`.deb`/`.rpm` assets, all built by `.github/workflows/release.yml`.

> Human-oriented diagrams (system overview, upload/WS sequences):
> `../docs/internal/architecture.md`.
> Decision rationale: [decisions.md](decisions.md) →
> `../../hsync-common/docs/internal/adr/`.
