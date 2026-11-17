# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L2 — REST API latency and concurrency benchmark (real server, wire-only).

Spawns the real hsync-server and drives it over HTTP only: the same binary a
client talks to, no in-process imports of the server. It fills the gap the L1
micro-benchmarks cannot see and the L3 soak reports only in aggregate:

* sequential latency per endpoint (``/health``, listing, upload, download);
* the concurrency curve for the two hot read paths (listing, download) with the
  node's CPU% (100% = one core), which exposes the single-process GIL ceiling.

A listing is O(entries) CPU per client: the row with ``--files`` entries is the
one that shows it (and the listing-scaling limit tracked in the wiki).

Loopback numbers are comparative (Python v1 vs the future C port, before/after a
change), not production SLAs. Rate limits are raised by ``server_process`` so the
measurement is capacity, not admission control.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_api.py \
        [--files 1000] [--requests 20] [--levels 1,4,8,16] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from common import (
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    print_table,
)
from server_process import (
    API_TOKEN,
    cpu_seconds,
    free_port,
    rss_mb,
    start_server,
    wait_ready,
)

_PRECOMPRESSED_SUFFIX = ".zip"  # skips the server's zstd path (should_compress)


def _payload(size: int, seed: str) -> bytes:
    """Deterministic, effectively incompressible payload of *size* bytes."""
    digest = hashlib.sha256(seed.encode()).digest()
    return (digest * ((size // len(digest)) + 1))[:size]


def _upload(client: httpx.Client, bucket: str, name: str, content: bytes, cid: str) -> None:
    """Upload one file through the public multipart endpoint."""
    response = client.post(
        f"/api/v1/buckets/{bucket}/files",
        data={
            "cid": cid,
            "path": "",
            "sha256sum": hashlib.sha256(content).hexdigest(),
            "permissions": "644",
            "offset": "0",
            "size": "0",
            "compressed": "false",
            "vclock": "",
            "version_id": "",
        },
        files={"file": (name, content, "application/octet-stream")},
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"upload {name} failed: {response.status_code} {response.text[:200]}")


def _download(client: httpx.Client, bucket: str, name: str, expected: int, cid: str) -> None:
    """Download one file, consuming the raw wire body (no client decoding)."""
    with client.stream(
        "GET",
        f"/api/v1/buckets/{bucket}/files/download",
        params={"filename": name, "cid": cid},
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"download {name} failed: {response.status_code}")
        received = sum(len(chunk) for chunk in response.iter_raw())
    if received != expected:
        raise RuntimeError(f"download {name}: {received} bytes, expected {expected}")


def _listing(client: httpx.Client, bucket: str) -> None:
    """Fetch the whole listing (the response is streamed and parsed as JSON)."""
    response = client.get(f"/api/v1/buckets/{bucket}/files")
    if response.status_code != 200:
        raise RuntimeError(f"listing failed: {response.status_code}")


def _level(
    name: str,
    workers: int,
    per_worker: int,
    pid: int,
    op,
) -> BenchResult:
    """Run *workers* × *per_worker* calls and report latency + node CPU%."""

    def worker(_: int) -> list[float]:
        samples: list[float] = []
        for _ in range(per_worker):
            start = time.perf_counter()
            op()
            samples.append(time.perf_counter() - start)
        return samples

    cpu_before = cpu_seconds(pid)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        samples = [sample for chunk in pool.map(worker, range(workers)) for sample in chunk]
    wall = time.perf_counter() - started
    cpu = cpu_seconds(pid) - cpu_before

    result = BenchResult(f"{name} conc={workers}", samples, len(samples))
    print(
        f"  {name:22s} conc={workers:2d}  ops/s={len(samples) / wall:8.1f}  "
        f"p50={result.p50:8.2f} ms  p99={result.p99:8.2f} ms  "
        f"node_cpu={100 * cpu / wall:5.0f}%"
    )
    return result


def run(
    args: argparse.Namespace, root: Path, bucket: str, cid: str
) -> tuple[list[BenchResult], int]:
    """Seed the bucket and measure the sequential + concurrent rows."""
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = root / "server.log"
    proc = start_server(port, root, log_path)
    results: list[BenchResult] = []
    try:
        wait_ready(proc, base_url)
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=300.0,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
        ) as client:
            response = client.post("/api/v1/buckets", params={"bucket": bucket})
            if response.status_code not in (200, 201):
                raise RuntimeError(f"bucket create failed: {response.status_code}")

            # Seed: --files 1 KiB entries, unique names so every upload is a create.
            seed_size = 1024
            seed_content = _payload(seed_size, "seed")
            started = time.perf_counter()
            for index in range(args.files):
                _upload(client, bucket, f"seed/file-{index:05d}.bin", seed_content, cid)
            elapsed = time.perf_counter() - started
            print(
                f"seeded {args.files} files in {elapsed:.1f}s "
                f"({args.files / elapsed:.1f} files/s) rss={rss_mb(proc.pid):.0f}MB"
            )

            upload_content = _payload(64 * 1024, "upload")
            download_content = _payload(1024 * 1024, "download")
            _upload(client, bucket, f"probe{_PRECOMPRESSED_SUFFIX}", download_content, cid)
            upload_count = {"n": 0}

            def op_upload() -> None:
                upload_count["n"] += 1
                _upload(
                    client,
                    bucket,
                    f"probe-{upload_count['n']:05d}.bin",
                    upload_content,
                    cid,
                )

            results.append(measure("GET /health", lambda: client.get("/health"), count=200))
            results.append(
                measure(
                    f"GET /files ({args.files} entries)",
                    lambda: _listing(client, bucket),
                    count=30,
                )
            )
            results.append(measure("upload 64KiB", op_upload, count=20))
            results.append(
                measure(
                    "download 1MiB (raw)",
                    lambda: _download(
                        client, bucket, f"probe{_PRECOMPRESSED_SUFFIX}", len(download_content), cid
                    ),
                    count=10,
                )
            )

            levels = [int(level) for level in args.levels.split(",") if level.strip()]
            for workers in levels:
                results.append(
                    _level(
                        f"list({args.files})",
                        workers,
                        args.requests,
                        proc.pid,
                        lambda: _listing(client, bucket),
                    )
                )
            for workers in levels:
                results.append(
                    _level(
                        "download 1MiB",
                        workers,
                        args.requests,
                        proc.pid,
                        lambda: _download(
                            client,
                            bucket,
                            f"probe{_PRECOMPRESSED_SUFFIX}",
                            len(download_content),
                            cid,
                        ),
                    )
                )
            return results, proc.pid
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - best-effort teardown of a bench process
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files", type=int, default=1000, help="Entries seeded for the listing rows."
    )
    parser.add_argument("--requests", type=int, default=20, help="Requests per worker and level.")
    parser.add_argument(
        "--levels", default="1,4,8,16", help="Concurrency levels (comma-separated)."
    )
    add_json_out(parser)
    args = parser.parse_args()

    bucket = f"perf-{uuid.uuid4().hex[:10]}"
    cid = "BenchClient1234567890"  # 21 alphanumeric chars per CID_PATTERN
    with tempfile.TemporaryDirectory(prefix="hsync-bench-api-") as tmp:
        results, pid = run(args, Path(tmp), bucket, cid)

    print(f"\nREST API latency (loopback, node pid {pid}; comparative, not an SLA)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
