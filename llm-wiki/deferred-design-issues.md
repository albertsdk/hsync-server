# Deferred Design Issues — hsync-server

> **Created:** 2026-08-11
> **Status:** Active

Issues that require an architectural choice before implementation.
Once a decision is made, convert the chosen option into an ADR under
`../../hsync-common/docs/internal/adr/` and implement accordingly.

---

## DI-02: Compressed Download Lacks `Content-Length`

### Current behaviour

`api/files/download.py::download_file`

When a download is served with zstd compression, the response uses
`StreamingResponse` (`api/files/download.py::download_file`) which does not include a
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

The `X-Checksum-SHA256` header is already set (`api/files/download.py::download_file`), confirming
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

## DI-03: File Listing Memory Bound — **CLOSED (2026-10-23)**

**Resolution**: the `/files` and `/dirs` listings are **streamed** as a chunked
JSON array (`StreamingResponse` over a sync generator,
`hsync_common/listing.py::iter_json_array`), produced one entry at a time on a worker
thread: the server holds O(directory fanout + one entry) instead of one dict per
file. Entries are emitted in deterministic **path order**; the Python client
(`hsync_client/sync.py::get_files_remote`) and the Android client (the
`SyncRepository.fetchRemoteFilesOrThrow` and `P2PSync.fetchRemoteFileList`
mappers) collect the whole array and sort by `mtime` to keep the oldest-first
priority. A failure mid-walk truncates the array (invalid JSON), so a consumer
fails loudly rather than acting on a partial listing. The P2P masters
(`hsync_client/p2p_server.py::_handle_list_files` and the Android
`P2PServer.handleListFiles`) stream the same shape (chunked; Ktor/aiohttp frame
it natively, the stdlib transport writes the chunked framing). Streaming rules
out both pagination (cursor state, inter-page gaps) and a `Content-Length`
bound, and keeps the body a valid JSON array, so no client parser changes. The
listing peak-memory bound is pinned by
`tests/test_memory_bounds.py::TestListingStreaming`.

**Bound**: the per-request peak is O(directory fanout + one entry) instead of
O(bucket); the exact per-entry allocator figure is intentionally not pinned
(`tests/test_memory_bounds.py::TestListingStreaming` verifies the bound, not a
byte count). The listing contract is documented in
[modules/rest-api.md](modules/rest-api.md).
