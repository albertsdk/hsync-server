# HSYNC Server — Decisions

Ecosystem-wide ADRs are recorded once under
[`../../hsync-common/docs/internal/adr/`](../../hsync-common/docs/internal/adr/README.md). This page
maps the ones that constrain the server to the server code that honors them, plus
the server-local choices not big enough for an ADR.

## ADRs that bind the server

| ADR | Server code that implements it |
| --- | ------------------------------ |
| [0001 server-vs-p2p-modes](../../hsync-common/docs/internal/adr/0001-server-vs-p2p-modes.md) | this whole process is the REST/WS server half; p2p is client-only |
| [0003 tombstone-gossip](../../hsync-common/docs/internal/adr/0003-tombstone-gossip.md) | `tombstones.py`; `GET/POST .../tombstones` (`api/files/list.py::get_tombstones`, `api/files/list.py::post_tombstones`) |
| [0005 protocol-versioning](../../hsync-common/docs/internal/adr/0005-protocol-versioning.md) | REST `VersionMiddleware` hard-fail 426 + malformed `X-Client-Name` 400 (`middleware.py::VersionMiddleware`); WS handshake mismatch (`api/events.py`) |
| [0022 causal-vector-clock-conflict-model](../../hsync-common/docs/internal/adr/0022-causal-vector-clock-conflict-model.md) | per-bucket version stores (`versions.py`) + causal upload arbitration (`arbitration.py`, shared by upload and delta-apply, `FileLockManager` critical section); SHA-256 dedup (`hashes.py`) + checksum verify on upload |
| [0023 corrected-mtime-tie-break](../../hsync-common/docs/internal/adr/0023-corrected-mtime-tie-break.md) | same arbitration core, now with the chronological tie-break: `arbitrate_upload` takes `incoming_corrected_mtime` (form field on upload and metadata on delta/apply), compares it with the stored `corrected_mtime`, falls back to SHA-256 when unknown/tied, records the winner via `record_version` and exposes it in listings (`server_version_info`) and the 201 `version` object |
| [0025 sqlite-external-state-store](../../hsync-common/docs/internal/adr/0025-sqlite-external-state-store.md) | `get_state_dir()`; `versions.py` / `tombstones.py` (per-bucket DB `server/<bucket>.db`, no in-bucket marker); `.hsyncnodeid` next to the state dir |

## Server-local decisions

- **Filesystem for user data; SQLite for causal metadata.** Bucket files live
  under `get_buckets_path()`. Versions and tombstones live in per-bucket DBs
  `get_state_dir()/server/<bucket>.db` (ADR-0025). SHA-256 cache is still in-memory
  and rebuilt at startup (`main.py::lifespan` → `hashes.py::set_sha256_hashes`).
- **Per-bucket WS pools** (`api/connection_manager.py::BucketConnectionManager`) instead of one global hub:
  isolation + smaller broadcast fan-out; global slot accounting still caps total
  and per-IP connections (`config.py::get_ws_max_connections` / `config.py::get_ws_max_connections_per_ip`).
- **Single event loop; blocking work offloaded to threads.** The server is
  deployed as one uvicorn worker (`HSYNC_WORKERS=1`; the WS registry and
  presence counters are in-process, `api/connection_manager.py::BucketConnectionManager`). Blocking I/O
  and GIL-releasing CPU work run via `asyncio.to_thread` (file hashing,
  decompression, delta signatures/match, scrypt token verify, fsync);
  compressed downloads use a dedicated CPU-sized pool (`compression.py::_COMPRESS_MAX_WORKERS`):
  `async_compress_stream` (`compression.py::async_compress_stream`) reads on the loop via
  `aiofiles` and offloads one ~1 MiB batch at a time, holding a worker only
  while a batch is compressed — never while the consumer is parked on socket
  backpressure — so a small pool serves many concurrent slow downloads without
  starvation. **Not offloaded**: state-DB writes (`record_version` /
  `record_deletion`, p50 ~21 us in `bench/bench_versions.py` — cheaper than the
  `to_thread` hop) and the delete/upload arbitration critical sections
  (atomicity). The upload's fused SHA-256 stays on the loop: it is a deliberate
  single-pass optimisation and hashlib releases the GIL on large buffers; the
  pre-upload backup snapshot is deliberately synchronous because it runs
  *outside* the per-path lock and must stay atomic against the loop.
  **Not adopted**: multi-process fan-out (`HSYNC_WORKERS>1`) — it
  would break WS fan-out, per-file `asyncio.Lock` serialization and the
  in-process hash/delta/auth caches.
- **Per-account tokens + bucket membership** (`auth.py` → `auth_store/`) —
  `hs_<id>_<secret>` tokens with roles (`admin`/`client`) and per-bucket
  `owner`/`member` membership; scrypt-hashed secrets, KDF cache, per-request
  DB re-check. Replaces the single shared API key (ADR-0008).
- **Persisted self-signed service identity** (`ssl_utils.py`, ADR-0037): ECDSA
  P-256, an unencrypted `0600` key in a `0700` directory, 20-year validity, so
  the TLS fingerprint is stable for client pinning across restarts; rotation via
  `admin tls rotate`, with an expiry warning at startup.
- **Resumable uploads via `.part` + fsync** (`api/files/upload.py`) for reliability
  on flaky NAS links.
- **Tag-driven GHCR release** (`.github/workflows/release.yml`): pushing a
  `vX.Y.Z` tag builds and pushes a multi-arch (`linux/amd64`, `linux/arm64`)
  image to `ghcr.io/albertsdk/hsync-server`,   tagged with the version and
  `latest`, then publishes the version-stamped `install.sh`, `compose.yaml`,
  `SHA256SUMS`, the Linux native binaries (`linux-amd64`/`linux-arm64`) and the
  `.deb` packages (`amd64`/`arm64`/`armhf`) as GitHub release assets — the same
  asset set as `hsync-bootnode`. Build context is the repo root with
  `../hsync-common` checked out alongside, matching `docker.sh`/`Dockerfile`.
  `install.sh` generates local secrets and deploys that pinned image; the
  container image remains the primary distribution (a standalone executable is
  also available — see below).
- **Fixed-name `latest` aliases**: the `release` job copies each versioned asset
  to a stable name (`hsync-server-linux-amd64.tar.gz`, `hsync-server-amd64.deb`,
  `hsync-server-x86_64.rpm`, …) so the docs link
  `releases/latest/download/<name>` without a version (`RELEASES.md` §5.8);
  guarded by `tests/test_compile_script.py`.
- **`install.sh --version latest`**: the installer resolves the latest stable
  GitHub Release by following the `github.com/<repo>/releases/latest` redirect
  with `curl -fsSIL -w %{url_effective}` (no GitHub API, no rate limit; drafts
  and pre-releases are excluded by GitHub), then pins the **resolved** `vX.Y.Z`
  tag in `.env` — the floating GHCR `:latest` tag is deliberately not written
  to `.env`, so a plain `docker compose pull` never upgrades implicitly
  (`install.sh`, `resolve_latest_version`).
- **Uniform `run.sh` (clone-and-run)**: a thin wrapper (`run.sh`) that checks the
  sibling `../hsync-common`, creates the in-project `.venv` via Poetry (main
  group only) and execs `hsync-server` — the mirror of `hsync-bootnode/run.sh`.
  The four installation methods are one page per method
  (`docs/getting-started-<script|docker|deb|source>.md`) behind a link-only
  chooser (`docs/getting-started.md`), kept identical in shape to the bootnode.
- **Standalone binary build** (`compile.sh`, targets `native`/`rpi`): a PyInstaller
  onefile freeze mirroring the client/bootnode build scripts. `native` uses the
  host virtualenv's Poetry `build` group; `rpi` cross-builds armhf/arm64 in
  Docker from `packaging/rpi/Dockerfile.builder` (Python 3.12, Poetry, and the
  Rust toolchain needed for the armhf `cryptography` source build) under
  `qemu-user` binfmt. The frozen binary speaks the same CLI/protocol as
  `python -m hsync_server`; the container image stays the primary distribution.
- **Debian packaging** (`compile.sh`, targets `deb`/`deb-all`): a `--onedir`
  PyInstaller freeze staged as a `.deb`, reusing the packaging machinery of
  hsync-client/hsync-bootnode (`packaging/deb/`, `_semver_to_debian`,
  `_finalize_deb_stage`, `_inject_shlib_depends` via `dpkg-shlibdeps`). The
  package installs under `/usr/lib/hsync-server` (with `/usr/bin/hsync-server`),
  `hsync-server.service`, a `/etc/hsync-server/hsync-server.conf` conffile and a
  dedicated `_hsync-server` user; the unit is registered but not enabled, and
  `HSYNC_ADMIN_KEY` is left for the operator to set. `deb-all` cross-builds
  amd64/arm64/armhf inside Docker. Not yet wired into the release workflow (the
  `.deb` is a local build artifact for now).
  The cross-build image pins its build inputs: the privileged `qemu-user-static`
  binfmt image is pulled by digest and Rust is installed from a SHA256-verified,
  version-pinned `rustup-init` rather than piping a remote script to a shell.
  The builder base is the official multi-arch `python:3.12-bookworm` image
  (prebuilt CPython), so the arm64 build no longer compiles CPython under
  qemu-user (where host qemu-aarch64 7.2.0 segfaults).
- **Container builds install the `build` group explicitly** (`rpi`/`deb-all`):
  `poetry install --only main,build`. Under Poetry 2.x (2.3.2, pinned in the
  builder image) a co-present `--with` is ignored inside `--only`, so the
  previous `--only main --with build` silently installed no PyInstaller (exit
  code 0, `pyinstaller` absent on PATH) and the cross-build targets aborted;
  guarded by `tests/test_compile_script.py`.

## House rules (from `AGENTS.md`)

- **Zero backward compatibility**: a protocol-version mismatch is a hard fail, not
  a negotiation. No legacy endpoints.
- **SSOT reuse**: models, protocol, validation, permissions come from
  `hsync_common`; the server never forks them.
- Changes to `HSYNC_*` flags / behaviors require the synchronized WD My Cloud OS5
  packaging update (see `AGENTS.md` domain-critical rules) and a docs update in
  both `llm-wiki/` and `docs/`.
