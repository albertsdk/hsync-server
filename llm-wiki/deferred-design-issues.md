# Deferred Design Issues — hsync-server

> **Created:** 2026-08-11
> **Status:** Analysed, deferred for architectural decision
> **Reference:** Test audit report (Problemi 4 e 6 di 7)

Issues that require an architectural choice before implementation.
Once a decision is made, convert the chosen option into an ADR under
`../hsync-common/docs/internal/adr/` and implement accordingly.

---

## DI-01: No Conflict Detection — Silent Last Writer Wins

### Current behaviour

`hsync_server/api/files/upload.py:234-267`

When two clients upload different content for the same file path, the last
one to arrive overwrites the earlier one silently. A `.backup` file is created
before the overwrite (`upload.py:265-267`), but it only protects against a
**crash during the upload itself** — it is deleted on success (`:450-451`). It
does NOT serve as a conflicted-copy mechanism.

There is no version field, vector clock, or modification-timestamp comparison
to detect that the file was changed concurrently.

### Why this matters (file sync domain)

- **Dropbox**: generates `filename (Conflicted Copy yyyy-mm-dd).ext` when two
  peers modify the same file concurrently.
- **Syncthing**: uses block-level version vectors; conflicts produce a
  `.sync-conflict-<date>-<time>` copy.
- **Nextcloud**: uses `oc_filecache` versioning; conflicts are flagged and
  both versions are kept.
- **rsync/rclone**: no built-in conflict resolution; conflict handling is the
  caller's responsibility.

Silent data loss on concurrent modification is a known anti-pattern in
multi-device sync systems.

### Options

| # | Strategy | Server change | Client change | Complexity |
|---|----------|--------------|---------------|------------|
| A | **Document LWW** — accept the current behaviour as a conscious choice, document it clearly | None | None | None |
| B | **Conflict Detection (version/mtime)** — client sends expected `mtime`/`version`; server rejects upload (409 + current metadata) if server-side is newer; client resolves | Add optional `expected_mtime` form field; compare before overwrite | Send expected mtime; handle 409 with reconciliation | Low-Medium |
| C | **Conflicted Copy (Dropbox-style)** — server renames existing file to `file_conflicted_copy_<timestamp>.ext` and accepts new upload; both survive | Rename existing before overwrite; never delete backup on success | None required (transparent) | Medium |
| D | **Vector Clocks / CRDT** — full distributed versioning; causal ordering | Major: new `version` model, merge logic, tombstone integration | Major: vector clock tracking, merge resolution | High |

### Client-side impact (hsync-client audit — 2026-08-11)

The client's reconciliation engine shares the same LWW vulnerability:

- **`hsync_client/sync.py:916-929`** (`get_sync_actions`): uses `mtime` as the
  primary tiebreaker to decide upload vs. download direction (2-second tolerance
  for FAT32 precision). No version clock or causal ordering.
- **`hsync_client/sync_state.py`**: persists merge-base hashes (three-way merge
  state) but these are content-hash snapshots, not version vectors — they detect
  *what* changed, not *when* relative to other peers.
- **`hsync_client/tombstones.py`**: uses LWW merge via `TombstoneStore.merge()`
  — identical to the server.

**Implication:** Any conflict-resolution strategy chosen for the server MUST be
implemented in the client simultaneously. The two sides negotiate sync direction
together; a version-aware server with a version-unaware client would still
silently lose data on concurrent modification.

### Decision needed

1. Choose one strategy (A, B, C, D).
2. If not A: write an ADR under `hsync-common/docs/internal/adr/`.
3. Implement server-side changes **and** client-side changes atomically:
   - Server: wire contract (new field/header), conflict detection/response.
   - Client (`hsync-client`): send version metadata, handle conflict responses,
     update `get_sync_actions()` to use version clocks instead of wall-clock mtime.
   - Android (`hsync-android`): mirror client changes.
4. Add black-box tests for the new conflict behaviour on both sides.
5. Update `docs/` (user-facing) and `llm-wiki/` (agent docs).
---

## DI-02: Compressed Download Lacks `Content-Length`

### Current behaviour

`hsync_server/api/files/download.py:132-145`

When a download is served with zstd compression, the response uses
`StreamingResponse` (`download.py:137`) which does not include a
`Content-Length` header. The compressed size is not known a priori because
compression happens chunk-by-chunk in a streaming fashion.

### Why this matters (file sync domain)

- **Resumable downloads**: without `Content-Length`, a client cannot know the
  total transfer size, making progress bars inaccurate and resume-after-
  interruption harder.
- **Disk pre-allocation**: a sync client that pre-allocates space before
  downloading (to avoid partial-write fragmentation) needs the final size.
- **Bandwidth planning**: multi-file sync queues benefit from knowing each
  file's transfer size to schedule optimally.

The `X-Checksum-SHA256` header is already set (`download.py:105`), confirming
the server knows the original file metadata.

### Options

| # | Strategy | Pros | Cons |
|---|----------|------|------|
| A | **Document status quo** — accept no `Content-Length` as a tradeoff for zero-latency streaming compression | No code change; lowest latency | No progress bar; no resume support for compressed downloads |
| B | **Pre-compress to temp file** — compress the whole file into a `.tmp` first, then serve with `Content-Length` and `FileResponse` | Accurate `Content-Length`; resumable via Range | Added latency before first byte; temp file disk I/O |
| C | **`X-Uncompressed-Size` header** — add a custom header with the original (uncompressed) size; client approximates progress using typical zstd ratio (~2-3x) | No latency penalty; useful estimate | Inaccurate progress; no resume support |
| D | **Hybrid threshold** — use streaming for files < `HSYNC_COMPRESSION_STREAMING_THRESHOLD` (default 10 MB), pre-compress for larger files | Good UX for small files; accurate progress for large transfers | Slightly more complex; still no progress for small files |

### Decision needed

1. Choose one strategy (A, B, C, D).
2. If not A: update `download.py`.
3. Add tests verifying the chosen header behaviour.
4. Document the client-facing contract in `docs/`.

---

## Reference

- Full test audit: commit `debcd90` on `main`
- Implemented fixes (Problems 1, 2, 3, 5, 7): same commit
- ADR template: `hsync-common/docs/internal/adr/`
