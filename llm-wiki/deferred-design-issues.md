# Deferred Design Issues — hsync-server

> **Created:** 2026-08-11
> **Status:** Analysed, deferred for architectural decision
> **Reference:** Test audit report (Problemi 4 e 6 di 7)

Issues that require an architectural choice before implementation.
Once a decision is made, convert the chosen option into an ADR under
`../hsync-common/docs/internal/adr/` and implement accordingly.

---

## ~~DI-01~~ → RESOLVED (Option C — Conflicted Copy, 2026-08-11)

### Resolution

Adopted **Option C — Conflicted Copy (Dropbox-style)**. See
[ADR-0021](../../hsync-common/docs/internal/adr/0021-server-conflicted-copy.md)
for the full decision rationale.

### What changed

`hsync_server/api/files/upload.py:451-476`: when an upload overwrites a file
with **different content** (different SHA-256), the server now renames the
previous version to `filename_conflicted_copy_<ISO8601>_<cid>.ext` instead of
silently deleting it. The existing `.backup` mechanism (created for crash
protection at `upload.py:267`) is repurposed for this.

- **No change to the wire protocol** — the HTTP API is unchanged.
- **No change to client code required** — the conflicted copy appears as a new
  file in the bucket listing and is synced to all peers normally.
- **Server-only change** — the client retains the existing `mtime`-based
  reconciliation (ADR-0006 already defines `resolve_three_way` for the client
  side; the server side was the missing piece).

### Original analysis (pre-resolution)

When two clients uploaded different content for the same file path, the last
writer overwrote the earlier one silently. The `.backup` file protected only
against a crash during upload and was deleted on success.

| Option | Chosen |
|--------|--------|
| A — Document LWW | No |
| B — Conflict Detection (mtime-based) | No |
| **C — Conflicted Copy (Dropbox-style)** | **Yes ✅** |
| D — Vector Clocks / CRDT | No |
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
