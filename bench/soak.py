# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L3 — server soak (MANUAL, not a test): sustained mixed REST traffic.

Spawns the real server (uvicorn, loopback, temp buckets/state) and drives it
with a mixed upload/overwrite/download-verify/list/delete+recreate workload
for --duration seconds, sampling the *server* process RSS and CPU from /proc.

Pass criteria: no failed operation, the final listing matches the expected
manifest (every file downloads and re-hashes), and the RSS delta stays under
--max-rss-mb. Rate limiting is raised for the soak (it measures sustained
throughput and memory growth; the BB suite covers rate-limit behaviour).

Never imports ``hsync_server``: the harness must stay usable if the server
internals are rewritten (bench convention, see bench/README.md).

With download compression enabled the soak also exercises the per-stream
compressor isolation under sustained load: a compression-ON run reaches
SOAK-PASS. ``--no-compression`` isolates harness/RSS measurements from the
compression path.

Usage:
    cd hsync-server && .venv/bin/python bench/soak.py \
        [--duration 600] [--threads 4] [--files 200] [--file-kb 64]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Fixed bootstrap admin secret (64 hex) for every soak run; the Bearer token is
# hs_HsyncAdminBootstrap01_<secret>.
_ADMIN_SECRET = "0123456789abcdef" * 4  # 64 hex chars, fixed and safe
_TOKEN = f"hs_HsyncAdminBootstrap01_{_ADMIN_SECRET}"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _proc_rss_mb(pid: int) -> float:
    """Resident set size of *pid* in MiB (0.0 when the process is gone)."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return 0.0
    return 0.0


def _proc_cpu_seconds(pid: int) -> float:
    """utime+stime of *pid* in seconds (comm may contain spaces: split after ')')."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return 0.0
    fields = stat[stat.rindex(")") + 2 :].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[11]) + int(fields[12])) / ticks


def _payload(seed: str, size: int) -> bytes:
    """Deterministic per-file payload of *size* bytes."""
    digest = hashlib.sha256(seed.encode()).digest()
    return (digest * ((size // len(digest)) + 1))[:size]


def _tick_clock(clock: dict[str, int], cid: str) -> dict[str, int]:
    """Return *clock* with *cid*'s counter advanced by one.

    A real client uploads with the causal history it has merged so far, ticked
    by its own cid; an empty vclock would make every overwrite a concurrent
    tie-break, and the server would preserve the loser as a conflicted copy.
    """
    ticked = dict(clock)
    ticked[cid] = ticked.get(cid, 0) + 1
    return ticked


def _upload(
    client: httpx.Client, bucket: str, rel: str, data: bytes, cid: str, vclock: str = ""
) -> httpx.Response:
    name = rel.rsplit("/", 1)[-1]
    path = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return client.post(
        f"/api/v1/buckets/{bucket}/files",
        data={
            "cid": cid,
            "path": path,
            "sha256sum": hashlib.sha256(data).hexdigest(),
            "permissions": "644",
            "offset": "0",
            "size": str(len(data)),
            "compressed": "false",
            "vclock": vclock,
        },
        files={"file": (name, data, "application/octet-stream")},
    )


def _download(client: httpx.Client, bucket: str, rel: str) -> httpx.Response:
    return client.get(
        f"/api/v1/buckets/{bucket}/files/download",
        params={"filename": rel},
        headers={"Accept-Encoding": "identity"},
    )


@dataclass
class SoakState:
    """Shared counters, expected manifest and per-worker file ownership."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    expected: dict[str, bytes] = field(default_factory=dict)
    clocks: dict[str, dict[str, int]] = field(default_factory=dict)
    owned: list[list[str]] = field(default_factory=list)
    ops: int = 0
    errors: int = 0
    printed_errors: int = 0

    def record_error(self, op: str, status: object, detail: str) -> None:
        with self.lock:
            self.errors += 1
            if self.printed_errors < 20:
                self.printed_errors += 1
                print(f"  op error [{op}] status={status}: {detail}")

    def bump_ops(self) -> None:
        with self.lock:
            self.ops += 1

    def set_expected(self, rel: str, data: bytes) -> None:
        with self.lock:
            self.expected[rel] = data

    def get_expected(self, rel: str) -> bytes | None:
        with self.lock:
            return self.expected.get(rel)


def _worker(
    state: SoakState,
    base_url: str,
    bucket: str,
    tid: int,
    size: int,
    cid: str,
    stop: threading.Event,
    verify_every: int,
) -> None:
    """One concurrent client: mixed workload on the files it owns."""
    owned = state.owned[tid]
    # Per-file causal history this worker has merged (it owns these files).
    clocks: dict[str, dict[str, int]] = {rel: dict(state.clocks.get(rel, {})) for rel in owned}
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=60.0) as client:
        tick = 0
        pick = 0
        while not stop.is_set():
            tick += 1
            kind = tick % 10
            if 4 <= kind < 7 and tick % verify_every:
                continue  # sampled download not due on this tick
            # File selection is independent of the op kind: a correlated
            # (rel, kind) mapping would pin every op kind to one fixed file.
            rel = owned[pick % len(owned)]
            pick += 1
            try:
                if kind < 4:  # overwrite with fresh content
                    data = _payload(f"{tid}:{rel}:{tick}", size)
                    clocks[rel] = _tick_clock(clocks[rel], cid)
                    r = _upload(client, bucket, rel, data, cid, json.dumps(clocks[rel]))
                    if r.status_code != 201:
                        state.record_error(f"upload {rel}", r.status_code, r.text[:120])
                        continue
                    state.set_expected(rel, data)
                elif kind < 7:  # sampled download + re-hash
                    expected = state.get_expected(rel)
                    if expected is None:
                        continue
                    r = _download(client, bucket, rel)
                    if r.status_code != 200:
                        state.record_error(f"download {rel}", r.status_code, r.text[:120])
                    elif r.content != expected:
                        got = hashlib.sha256(r.content).hexdigest()[:12]
                        want = hashlib.sha256(expected).hexdigest()[:12]
                        state.record_error(
                            f"download {rel}",
                            r.status_code,
                            f"content mismatch: {len(r.content)}B sha={got} "
                            f"vs {len(expected)}B sha={want}",
                        )
                elif kind < 9:  # listing stays consistent under writes
                    r = client.get(f"/api/v1/buckets/{bucket}/files")
                    if r.status_code != 200:
                        state.record_error("list", r.status_code, r.text[:120])
                else:  # delete + recreate (tombstone path)
                    r = client.request(
                        "DELETE",
                        f"/api/v1/buckets/{bucket}/files",
                        data={"cid": cid, "filename": rel},
                    )
                    if r.status_code != 200:
                        state.record_error(f"delete {rel}", r.status_code, r.text[:120])
                        continue
                    data = _payload(f"{tid}:{rel}:recreate:{tick}", size)
                    # Resolve the deletion in the clock, then tick for the write
                    # (the tombstone inherited the last version clock + 1 tick).
                    clocks[rel] = _tick_clock(_tick_clock(clocks[rel], cid), cid)
                    r = _upload(client, bucket, rel, data, cid, json.dumps(clocks[rel]))
                    if r.status_code != 201:
                        state.record_error(f"recreate {rel}", r.status_code, r.text[:120])
                        continue
                    state.set_expected(rel, data)
                state.bump_ops()
            except Exception as exc:  # noqa: BLE001 - soak must record, not crash
                state.record_error(f"op {kind}", "exception", f"{type(exc).__name__}: {exc}")


def _start_server(
    port: int, root: Path, log_path: Path, *, no_compression: bool = False
) -> subprocess.Popen:
    """Spawn the real server with a scrubbed, soak-specific environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    buckets_root = root / "buckets"
    state_root = root / "state"
    # The server refuses to start when the buckets dir is missing (the
    # lifespan preload validates it), so create both dirs up front.
    buckets_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HSYNC_ADMIN_KEY": _ADMIN_SECRET,
            "HSYNC_SCRYPT_N": "16",
            "HSYNC_SCRYPT_MAXMEM": "33554432",
            "HSYNC_BUCKETS": str(buckets_root),
            "HSYNC_STATE_DIR": str(state_root),
            "HSYNC_RATE_LIMIT_DEFAULT": "1000000/minute",
            "HSYNC_RATE_LIMIT_UPLOAD": "1000000/minute",
            "HSYNC_RATE_LIMIT_DOWNLOAD": "1000000/minute",
            "HSYNC_RATE_LIMIT_DELTA": "1000000/minute",
            "HSYNC_RATE_LIMIT_WEBSOCKET": "1000000/minute",
            "HSYNC_RATE_LIMIT_WS_MESSAGES": "1000000/second",
            "HSYNC_MIN_FREE_DISK_SPACE": "0",
            "HSYNC_ENABLE_DOCS": "false",
        }
    )
    if no_compression:
        env["HSYNC_COMPRESSION_ENABLED"] = "false"
    log = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hsync_server", "--port", str(port), "--log-level", "warning"],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()
    return proc


def _wait_ready(proc: subprocess.Popen, base_url: str, timeout: float = 60.0) -> None:
    """Poll ``GET /health`` until the server answers or the deadline passes."""
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited during startup (code {proc.returncode})")
            try:
                if client.get("/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    raise RuntimeError("server did not become healthy")


def _seed(state: SoakState, client: httpx.Client, bucket: str, files: int, size: int) -> None:
    """Upload the initial manifest through the public REST API."""
    for i in range(files):
        rel = f"soak/file_{i:04d}.bin"
        data = _payload(f"seed:{rel}", size)
        clock = {"SoakSeeder00000000001": 1}
        r = _upload(client, bucket, rel, data, "SoakSeeder00000000001", json.dumps(clock))
        if r.status_code != 201:
            raise RuntimeError(f"seed upload failed: {r.status_code} {r.text[:200]}")
        state.set_expected(rel, data)
        state.clocks[rel] = clock


def _final_verify(client: httpx.Client, bucket: str, expected: dict[str, bytes]) -> list[str]:
    """Listing parity + full download re-hash of the expected manifest."""
    problems: list[str] = []
    r = client.get(f"/api/v1/buckets/{bucket}/files")
    if r.status_code != 200:
        return [f"listing status {r.status_code}"]
    listed = {entry["filepath"] for entry in r.json()}
    problems += [f"missing: {rel}" for rel in sorted(set(expected) - listed)]
    problems += [f"unexpected: {rel}" for rel in sorted(listed - set(expected))]
    for rel, data in expected.items():
        d = _download(client, bucket, rel)
        if d.status_code != 200:
            problems.append(f"download {rel}: status {d.status_code}")
        elif d.content != data:
            problems.append(f"download {rel}: content mismatch")
    return problems


def run(args: argparse.Namespace) -> int:
    """Seed, hammer, sample and verify; return 0 on SOAK-PASS."""
    with tempfile.TemporaryDirectory(prefix="hsync-soak-") as tmp:
        root = Path(tmp)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        log_path = root / "server.log"
        proc = _start_server(port, root, log_path, no_compression=args.no_compression)
        try:
            _wait_ready(proc, base_url)
            headers = {"Authorization": f"Bearer {_TOKEN}"}
            size = args.file_kb * 1024
            state = SoakState()
            with httpx.Client(base_url=base_url, headers=headers, timeout=120.0) as client:
                r = client.post("/api/v1/buckets", params={"bucket": args.bucket})
                if r.status_code not in (200, 201):
                    print(f"bucket create failed: {r.status_code} {r.text[:200]}")
                    return 1
                _seed(state, client, args.bucket, args.files, size)
                state.owned = [
                    [rel for idx, rel in enumerate(state.expected) if idx % args.threads == t]
                    for t in range(args.threads)
                ]
                print(
                    f"seeded {args.files} files ({args.file_kb} KiB each), "
                    f"{args.threads} workers, {args.duration:.0f}s sustained phase, "
                    f"compression={'off' if args.no_compression else 'on'}"
                )
                rss_start = _proc_rss_mb(proc.pid)
                cpu_start = _proc_cpu_seconds(proc.pid)
                stop = threading.Event()
                workers = [
                    threading.Thread(
                        target=_worker,
                        args=(
                            state,
                            base_url,
                            args.bucket,
                            t,
                            size,
                            f"SoakWorker{t:011d}",
                            stop,
                            args.verify_every,
                        ),
                        daemon=True,
                    )
                    for t in range(args.threads)
                ]
                t0 = time.monotonic()
                for w in workers:
                    w.start()

                next_sample = t0 + args.sample_interval
                while time.monotonic() - t0 < args.duration:
                    time.sleep(0.2)
                    if time.monotonic() >= next_sample:
                        next_sample += args.sample_interval
                        print(
                            f"  t+{time.monotonic() - t0:5.0f}s ops={state.ops} "
                            f"errors={state.errors} rss={_proc_rss_mb(proc.pid):.0f}MB "
                            f"cpu={_proc_cpu_seconds(proc.pid) - cpu_start:.1f}s"
                        )
                stop.set()
                for w in workers:
                    w.join(timeout=30.0)

                problems = _final_verify(client, args.bucket, dict(state.expected))
                rss_end = _proc_rss_mb(proc.pid)
                elapsed = time.monotonic() - t0
                print(
                    f"stopped: ops={state.ops} ({state.ops / elapsed:.1f} ops/s) "
                    f"errors={state.errors} rss={rss_start:.0f}->{rss_end:.0f}MB"
                )
                for problem in problems[:10]:
                    print(f"  final mismatch: {problem}")
                ok = state.errors == 0 and not problems and (rss_end - rss_start) <= args.max_rss_mb
                print(
                    f"checks: errors={state.errors} mismatches={len(problems)} "
                    f"rss_delta={rss_end - rss_start:.0f}MB (limit {args.max_rss_mb:.0f}MB)"
                )
                print("SOAK-" + ("PASS" if ok else "FAIL"))
                if not ok:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                    print("server log tail:")
                    print(tail)
                return 0 if ok else 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--file-kb", type=int, default=64)
    parser.add_argument("--max-rss-mb", type=float, default=200.0)
    parser.add_argument("--sample-interval", type=float, default=15.0)
    parser.add_argument("--verify-every", type=int, default=4)
    parser.add_argument(
        "--no-compression",
        action="store_true",
        help="start the server with HSYNC_COMPRESSION_ENABLED=false (diagnostic: "
        "isolates harness/RSS checks from the compression path)",
    )
    parser.add_argument("--bucket", default="soak")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
