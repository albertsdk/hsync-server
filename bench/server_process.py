# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Start the real hsync-server for the load tiers (L2/L3).

Shared by ``bench/bench_api.py`` (L2) and ``bench/soak.py`` (L3) so both spawn
the same scrubbed, rate-limit-free instance and use the same readiness probe.
Nothing in the server or the test suite imports this module.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Per-process admin secret (64 hex): a committed/fixed credential on a server
# with rate limits off would be a real admin token for anyone who reaches the
# port. The Bearer token is hs_HsyncAdminBuiltinId01_<secret>.
ADMIN_SECRET = secrets.token_hex(32)
API_TOKEN = f"hs_HsyncAdminBuiltinId01_{ADMIN_SECRET}"


def payload(size: int, seed: str) -> bytes:
    """Deterministic, effectively incompressible payload of *size* bytes."""
    digest = hashlib.sha256(seed.encode()).digest()
    return (digest * ((size // len(digest)) + 1))[:size]


def free_port() -> int:
    """Return an ephemeral free TCP port on loopback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def rss_mb(pid: int) -> float:
    """Resident set size of *pid* in MiB (0 on an unreadable /proc)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return 0.0
    return 0.0


def cpu_seconds(pid: int) -> float:
    """utime+stime of *pid* in seconds (comm may contain spaces: split after ')')."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return 0.0
    fields = stat[stat.rindex(")") + 2 :].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[11]) + int(fields[12])) / ticks


def start_server(
    port: int, root: Path, log_path: Path, *, no_compression: bool = False
) -> subprocess.Popen:
    """Spawn the real server with a scrubbed, benchmark-specific environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    buckets_root = root / "buckets"
    state_root = root / "state"
    # The server refuses to start when the buckets dir is missing (the lifespan
    # preload validates it), so create both dirs up front.
    buckets_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HSYNC_ADMIN_KEY": ADMIN_SECRET,
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
            [
                sys.executable,
                "-m",
                "hsync_server",
                "--host",
                "127.0.0.1",  # loopback only: the bench server must never be reachable
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()
    return proc


def wait_ready(proc: subprocess.Popen, base_url: str, timeout: float = 60.0) -> None:
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
