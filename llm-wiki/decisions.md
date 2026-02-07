# HSYNC Server — Decisions

Ecosystem-wide ADRs are recorded once under
[`../../hsync-common/docs/internal/adr/`](../../hsync-common/docs/internal/adr/README.md). This page
maps the ones that constrain the server to the server code that honors them, plus
the server-local choices not big enough for an ADR.

## ADRs that bind the server

| ADR | Server code that implements it |
| --- | ------------------------------ |
| [0001 server-vs-lan-modes](../../hsync-common/docs/internal/adr/0001-server-vs-lan-modes.md) | this whole process is the REST/WS server half; LAN is client-only |
| [0003 tombstone-gossip](../../hsync-common/docs/internal/adr/0003-tombstone-gossip.md) | `tombstones.py`; `GET/POST .../tombstones` (`api/files/list.py:194`) |
| [0005 protocol-versioning](../../hsync-common/docs/internal/adr/0005-protocol-versioning.md) | REST `VersionMiddleware` hard-fail 426 (`middleware.py:111`); WS handshake mismatch (`api/events.py`) |
| [0022 causal-vector-clock-conflict-model](../../hsync-common/docs/internal/adr/0022-causal-vector-clock-conflict-model.md) | per-bucket version stores (`versions.py`) + causal upload arbitration (`arbitration.py`, shared by upload and delta-apply, `FileLockManager` critical section); SHA-256 dedup (`hashes.py`) + checksum verify on upload |
| [0023 corrected-mtime-tie-break](../../hsync-common/docs/internal/adr/0023-corrected-mtime-tie-break.md) | same arbitration core, now with the chronological tie-break: `arbitrate_upload` takes `incoming_corrected_mtime` (form field on upload and metadata on delta/apply), compares it with the stored `corrected_mtime`, falls back to SHA-256 when unknown/tied, records the winner via `record_version` and exposes it in listings (`server_version_info`) and the 201 `version` object |
| [0025 sqlite-external-state-store](../../hsync-common/docs/internal/adr/0025-sqlite-external-state-store.md) | `get_state_dir()`; `versions.py` / `tombstones.py` (`buckets` table, no in-bucket marker); `.hsyncnodeid` next to `syncstate.db` |

## Server-local decisions

- **Filesystem for user data; SQLite for causal metadata.** Bucket files live
  under `get_buckets_path()`. Versions and tombstones live in
  `get_state_dir()/syncstate.db` (ADR-0025). SHA-256 cache is still in-memory
  and rebuilt at startup (`main.py:58` → `hashes.py:73`).
- **Per-bucket WS pools** (`connection_manager.py:71`) instead of one global hub:
  isolation + smaller broadcast fan-out; global slot accounting still caps total
  and per-IP connections (`config.py:457`/`:462`).
- **Bearer API key, constant-time compare** (`routers.py:69`) — single shared
  secret model; no user accounts.
- **Persisted self-signed cert** (`ssl_utils.py:138`) so the TLS fingerprint is
  stable for peer TOFU pinning across restarts.
- **Resumable uploads via `.part` + fsync** (`api/files/upload.py`) for reliability
  on flaky NAS links.
- **Tag-driven GHCR release** (`.github/workflows/release.yml`): pushing a
  `vX.Y.Z` tag builds and pushes a multi-arch (`linux/amd64`, `linux/arm64`)
  image to `ghcr.io/albertobasso/hsync-server`, tagged with the version and
  `latest`, then publishes the version-stamped `install.sh`, `compose.yaml`, and
  `SHA256SUMS` as GitHub release assets. Build context is the repo root with
  `../hsync-common` checked out alongside, matching `docker.sh`/`Dockerfile`.
  `install.sh` generates local secrets and deploys that pinned image; the server
  still ships only as an image, not as a standalone binary.
- **`install.sh --version latest`**: the installer resolves the latest stable
  GitHub Release by following the `github.com/<repo>/releases/latest` redirect
  with `curl -fsSIL -w %{url_effective}` (no GitHub API, no rate limit; drafts
  and pre-releases are excluded by GitHub), then pins the **resolved** `vX.Y.Z`
  tag in `.env` — the floating GHCR `:latest` tag is deliberately not written
  to `.env`, so a plain `docker compose pull` never upgrades implicitly
  (`install.sh`, `resolve_latest_version`).

## House rules (from `CLAUDE.md`)

- **Zero backward compatibility**: a protocol-version mismatch is a hard fail, not
  a negotiation. No legacy endpoints.
- **SSOT reuse**: models, protocol, validation, permissions come from
  `hsync_common`; the server never forks them.
- Changes to `HSYNC_*` flags / behaviors require the synchronized WD My Cloud OS5
  packaging update (see `CLAUDE.md` domain-critical rules) and a docs update in
  both `llm-wiki/` and `docs/`.
