# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Startup benchmark for hsync-server (import + readiness).

Two process-spawn scenarios isolate the two phases of server startup:

* ``server_import_main`` — cold ``import hsync_server.main`` (FastAPI +
  instrumentator + slowapi + middleware) in a fresh process;
* ``server_startup_to_health`` — spawn ``.venv/bin/hsync-server`` against a fresh
  temp ``HSYNC_STATE_DIR``/``HSYNC_BUCKETS`` and measure wall-clock until
  ``GET /health`` returns 200. ``--files N`` pre-populates the buckets, so the
  delta between ``N=0`` and a realistic ``N`` isolates the ``set_sha256_hashes``
  preload cost of the lifespan.

The server is terminated (SIGTERM) after each sample, outside the timed window.
Standalone script, not collected by pytest, not run in CI; comparative figures.

Usage:
    cd hsync-server && .venv/bin/python bench/bench_startup.py --count 10
    .venv/bin/python bench/bench_startup.py --count 10 --files 5000 --json-out /tmp/server.json
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    isolate_server_state,
    maybe_write_json,
    measure,
    print_table,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_READY_TIMEOUT = 30.0


def _server_binary(python: str) -> str | None:
    candidate = Path(python).with_name("hsync-server")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HSYNC_")}
    env["NO_COLOR"] = "1"
    return env


def _populate(buckets: str, files: int) -> None:
    bucket_dir = Path(buckets) / "benchbucket"
    bucket_dir.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (bucket_dir / f"file_{index:06d}.bin").write_bytes(b"x")


def _wait_ready(port: int, deadline: float) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.05)
    return False


def _startup_to_health_sample(python: str, files: int) -> float:
    """Start the server, return wall-clock (s) until /health is ready, then stop it."""
    binary = _server_binary(python)
    if binary is None:
        raise RuntimeError("hsync-server console script not found next to the python interpreter")

    isolate_server_state()
    root = os.environ["HSYNC_STATE_DIR"]
    buckets = os.environ["HSYNC_BUCKETS"]
    _populate(buckets, files)

    port = _free_port()
    env = _clean_env()
    env["HSYNC_STATE_DIR"] = root
    env["HSYNC_BUCKETS"] = buckets

    proc = subprocess.Popen(
        [binary, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(_PROJECT_ROOT),
    )
    try:
        start = time.perf_counter()
        ready = _wait_ready(port, start + _READY_TIMEOUT)
        elapsed = time.perf_counter() - start
        if not ready:
            raise RuntimeError("server did not become ready within the timeout")
        return elapsed
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _measure_startup(name: str, python: str, files: int, count: int) -> BenchResult:
    samples: list[float] = []
    _startup_to_health_sample(python, files)  # warm-up
    for _ in range(count):
        samples.append(_startup_to_health_sample(python, files))
    return BenchResult(name, samples, count)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument(
        "--files",
        type=int,
        default=0,
        help="Pre-populated bucket files (isolates the hash preload).",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--no-startup", action="store_true", help="Skip the spawn-to-health scenario."
    )
    add_json_out(parser)
    args = parser.parse_args()

    print(
        f"hsync-server startup benchmark — count={args.count} files={args.files}",
        f"python {sys.version.split()[0]}",
    )
    print("comparative figures (this machine, warm page cache), not SLAs\n")

    results: list[BenchResult] = []

    def import_op() -> None:
        subprocess.run(
            [args.python, "-c", "import hsync_server.main"],
            capture_output=True,
            text=True,
            env=_clean_env(),
            cwd=str(_PROJECT_ROOT),
            check=True,
        )

    results.append(measure("server_import_main", import_op, count=args.count, warmup=1))

    if not args.no_startup:
        results.append(
            _measure_startup("server_startup_to_health", args.python, args.files, args.count)
        )

    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
