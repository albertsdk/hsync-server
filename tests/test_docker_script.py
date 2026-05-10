"""Tests for the Docker management shell wrapper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKER_SCRIPT = PROJECT_ROOT / "docker.sh"


def _base_env(tmp_path: Path, args_file: Path) -> dict[str, str]:
    """Return a deterministic environment with a fake docker executable first in PATH."""
    env = os.environ.copy()
    for key in (
        "HSYNC_ADMIN_KEY",
        "HSYNC_PORT",
        "HSYNC_LOG_LEVEL",
        "HSYNC_WORKERS",
        "HSYNC_TZ",
        "HSYNC_STARTUP_HEALTH_TIMEOUT",
        "HSYNC_UID",
        "HSYNC_GID",
        "HSYNC_DATA_DIR",
    ):
        env.pop(key, None)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["HSYNC_FAKE_DOCKER_ARGS"] = str(args_file)
    # Isolate the host data directory from the repository: `start` creates
    # and bind-mounts it, so it must live under the test's tmp dir.
    env["HSYNC_DATA_DIR"] = str(tmp_path / "data")
    # Unit tests use a fake docker (no real server): skip the post-start
    # /health poll so `start` returns immediately with the printed URLs.
    env["HSYNC_STARTUP_HEALTH_TIMEOUT"] = "0"
    return env


def _write_fake_docker(tmp_path: Path) -> Path:
    """Create a fake docker command that records non-query invocations."""
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "ps" ]]; then exit 0; fi\n'
        'printf \'%s\\n\' "$@" >> "$HSYNC_FAKE_DOCKER_ARGS"\n'
        "exit 0\n"
    )
    fake_docker.chmod(0o755)
    return fake_docker


def _run_script(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run docker.sh with fake docker and return process plus recorded docker args."""
    _write_fake_docker(tmp_path)
    args_file = tmp_path / "docker.args"
    env = _base_env(tmp_path, args_file)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [str(DOCKER_SCRIPT), *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    recorded = args_file.read_text().splitlines() if args_file.exists() else []
    return result, recorded


def test_help_does_not_require_docker_or_api_key(tmp_path: Path):
    result, recorded = _run_script(tmp_path, "--help")

    assert result.returncode == 0
    assert "HSYNC Server - Docker Management" in result.stdout
    assert recorded == []


def test_start_requires_api_key_when_no_env_file(tmp_path: Path):
    result, recorded = _run_script(tmp_path, "start")

    assert result.returncode == 1
    assert "HSYNC_ADMIN_KEY is required" in result.stderr
    assert recorded == []


def test_start_uses_env_file_without_leaking_api_key_value(tmp_path: Path):
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "HSYNC_ADMIN_KEY=test-placeholder-api-key-000000\nHSYNC_PORT=9999\nHSYNC_LOG_LEVEL=debug\n"
    )

    result, recorded = _run_script(tmp_path, "start", "--env-file", str(env_file))

    assert result.returncode == 0
    assert "Port: 9999" in result.stdout
    assert "Log Level: debug" in result.stdout
    assert "http://localhost:9999" in result.stdout
    assert "--env-file" in recorded
    assert str(env_file) in recorded
    assert "9999:9999" in recorded
    # Bind mounts from the host data directory to the canonical paths.
    assert f"{tmp_path}/data/state:/project/state" in recorded
    assert f"{tmp_path}/data/buckets:/project/buckets" in recorded
    assert f"{tmp_path}/data/certs:/project/certs" in recorded
    assert not any(arg.startswith("HSYNC_ADMIN_KEY=") for arg in recorded)


def test_start_passes_parent_api_key_by_name_only(tmp_path: Path):
    result, recorded = _run_script(
        tmp_path,
        "start",
        env_overrides={"HSYNC_ADMIN_KEY": "test-placeholder-api-key-000000"},
    )

    assert result.returncode == 0
    assert "-e" in recorded
    assert "HSYNC_ADMIN_KEY" in recorded
    assert "http://localhost:9753" in result.stdout
    assert f"{tmp_path}/data/state:/project/state" in recorded
    assert not any(arg.startswith("HSYNC_ADMIN_KEY=") for arg in recorded)


def test_start_creates_data_directory_tree(tmp_path: Path):
    result, _recorded = _run_script(
        tmp_path,
        "start",
        env_overrides={"HSYNC_ADMIN_KEY": "test-placeholder-api-key-000000"},
    )

    assert result.returncode == 0
    for name in ("buckets", "state", "certs"):
        assert (tmp_path / "data" / name).is_dir()


def test_clean_all_removes_data_directory(tmp_path: Path):
    result, _recorded = _run_script(
        tmp_path,
        "start",
        env_overrides={"HSYNC_ADMIN_KEY": "test-placeholder-api-key-000000"},
    )
    assert result.returncode == 0
    assert (tmp_path / "data").is_dir()

    result, _recorded = _run_script(tmp_path, "clean", "--all")

    assert result.returncode == 0
    assert not (tmp_path / "data").exists()


def test_build_uses_repository_root_as_context(tmp_path: Path):
    result, recorded = _run_script(tmp_path, "build")

    assert result.returncode == 0
    assert recorded[:2] == ["build", "-t"]
    assert "-f" in recorded
    assert str(PROJECT_ROOT / "Dockerfile") in recorded
    assert str(PROJECT_ROOT.parent) in recorded
