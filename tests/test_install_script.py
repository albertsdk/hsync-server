"""Tests for the release installation bootstrap."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"


def _write_fake_commands(tmp_path: Path, *, snap_docker: bool = False) -> Path:
    docker_args = tmp_path / "docker.args"
    # A snap-packaged docker is exposed through /snap/bin; mirror that layout so
    # the installer's snap detection sees the same path shape.
    docker_dir = tmp_path / "snap" / "bin" if snap_docker else tmp_path
    docker_dir.mkdir(parents=True, exist_ok=True)
    docker = docker_dir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "$HSYNC_FAKE_DOCKER_ARGS"\nexit 0\n'
    )
    docker.chmod(0o755)

    openssl = tmp_path / "openssl"
    openssl.write_text("#!/usr/bin/env bash\nprintf '0123456789abcdef%.0s' {1..4}\nprintf '\\n'\n")
    openssl.chmod(0o755)

    # Fake curl only answers the latest-release resolution; HSYNC_FAKE_CURL_FAIL
    # simulates a network error, HSYNC_FAKE_LATEST_TAG overrides the tag.
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        '[[ -z "${HSYNC_FAKE_CURL_FAIL:-}" ]] || exit 22\n'
        "printf 'https://github.com/albertsdk/hsync-server/releases/tag/%s\\n' "
        '"${HSYNC_FAKE_LATEST_TAG:-v9.9.9}"\n'
    )
    curl.chmod(0o755)
    return docker_args


def _run_installer(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    snap_docker: bool = False,
    install_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    docker_args = _write_fake_commands(tmp_path, snap_docker=snap_docker)
    if install_dir is None:
        install_dir = tmp_path / "installation"
    env = os.environ.copy()
    # Prepend both the plain and the snap-style bin dir; only one holds `docker`.
    env["PATH"] = os.pathsep.join([str(tmp_path), str(tmp_path / "snap" / "bin"), env["PATH"]])
    env["HSYNC_FAKE_DOCKER_ARGS"] = str(docker_args)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--install-dir", str(install_dir), *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    recorded = docker_args.read_text().splitlines() if docker_args.exists() else []
    return result, install_dir, recorded


def test_help_has_no_runtime_dependencies(tmp_path: Path):
    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--help"],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Install HSYNC Server with Docker Compose" in result.stdout


def test_dry_run_does_not_create_installation(tmp_path: Path):
    result, install_dir, recorded = _run_installer(tmp_path, "--dry-run")

    assert result.returncode == 0
    assert "Dry run" in result.stdout
    assert not install_dir.exists()
    assert recorded == []


def test_no_start_generates_canonical_compose_and_private_secrets(tmp_path: Path):
    result, install_dir, recorded = _run_installer(
        tmp_path,
        "--version",
        "v1.2.3",
        "--bind-address",
        "192.168.1.10",
        "--port",
        "9443",
        "--no-start",
    )

    assert result.returncode == 0, result.stderr
    assert (install_dir / "compose.yaml").read_text() == COMPOSE_FILE.read_text()
    env_file = install_dir / ".env"
    env_contents = env_file.read_text()
    assert "HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v1.2.3" in env_contents
    assert "HSYNC_BIND_ADDRESS=192.168.1.10" in env_contents
    assert "HSYNC_CERT_HOSTNAME=192.168.1.10" in env_contents
    assert "HSYNC_PUBLIC_PORT=9443" in env_contents
    assert "HSYNC_ADMIN_KEY=0123456789abcdef" in env_contents
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert recorded[:2] == ["compose", "version"]
    assert "config" in recorded
    assert "pull" not in recorded
    assert "up" not in recorded


def test_rerun_preserves_secrets_and_updates_selected_version(tmp_path: Path):
    first, install_dir, _ = _run_installer(
        tmp_path,
        "--bind-address",
        "192.168.1.20",
        "--port",
        "9443",
        "--no-start",
    )
    assert first.returncode == 0, first.stderr
    env_file = install_dir / ".env"
    original = env_file.read_text()

    second, _, _ = _run_installer(tmp_path, "--version", "v2.0.0", "--no-start")

    assert second.returncode == 0, second.stderr
    assert "Endpoint: https://192.168.1.20:9443" in second.stdout
    updated = env_file.read_text()
    api_key = next(line for line in original.splitlines() if line.startswith("HSYNC_ADMIN_KEY="))
    assert api_key in updated
    assert "HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v2.0.0" in updated
    assert "HSYNC_BIND_ADDRESS=192.168.1.20" in updated
    assert "HSYNC_CERT_HOSTNAME=192.168.1.20" in updated
    assert "HSYNC_PUBLIC_PORT=9443" in updated


def test_invalid_network_options_are_rejected_before_writing(tmp_path: Path):
    result, install_dir, recorded = _run_installer(
        tmp_path,
        "--bind-address",
        "example.com",
        "--port",
        "70000",
    )

    assert result.returncode == 1
    assert "invalid IPv4 bind address" in result.stderr
    assert not install_dir.exists()
    assert recorded == []


def test_wildcard_bind_is_rejected_because_it_cannot_identify_certificate(tmp_path: Path):
    result, install_dir, recorded = _run_installer(
        tmp_path,
        "--bind-address",
        "0.0.0.0",
    )

    assert result.returncode == 1
    assert "cannot identify the TLS certificate" in result.stderr
    assert not install_dir.exists()
    assert recorded == []


def test_rerun_rejects_bind_change_that_would_invalidate_persisted_certificate(
    tmp_path: Path,
):
    first, install_dir, _ = _run_installer(
        tmp_path,
        "--bind-address",
        "192.168.1.20",
        "--no-start",
    )
    assert first.returncode == 0, first.stderr

    second, _, _ = _run_installer(
        tmp_path,
        "--bind-address",
        "192.168.1.21",
        "--no-start",
    )

    assert second.returncode == 1
    assert "cannot change the bind address" in second.stderr
    assert "HSYNC_BIND_ADDRESS=192.168.1.20" in (install_dir / ".env").read_text()


def test_latest_resolves_to_the_stable_release_tag(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--version", "latest", "--no-start")

    assert result.returncode == 0, result.stderr
    assert "Image: ghcr.io/albertsdk/hsync-server:v9.9.9" in result.stdout
    env_contents = (install_dir / ".env").read_text()
    assert "HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v9.9.9" in env_contents


def test_latest_rerun_updates_existing_installation_to_the_resolved_tag(tmp_path: Path):
    first, install_dir, _ = _run_installer(
        tmp_path,
        "--version",
        "v1.2.3",
        "--no-start",
    )
    assert first.returncode == 0, first.stderr

    second, _, _ = _run_installer(tmp_path, "--version", "latest", "--no-start")

    assert second.returncode == 0, second.stderr
    env_contents = (install_dir / ".env").read_text()
    assert "HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v9.9.9" in env_contents


def test_latest_resolution_failure_writes_nothing(tmp_path: Path):
    result, install_dir, recorded = _run_installer(
        tmp_path,
        "--version",
        "latest",
        "--no-start",
        extra_env={"HSYNC_FAKE_CURL_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "could not resolve the latest release" in result.stderr
    assert not install_dir.exists()
    assert recorded == []


def test_latest_rejects_an_unexpected_tag_format(tmp_path: Path):
    result, install_dir, _ = _run_installer(
        tmp_path,
        "--version",
        "latest",
        "--no-start",
        extra_env={"HSYNC_FAKE_LATEST_TAG": "not-a-version"},
    )

    assert result.returncode == 1
    assert "unexpected latest release tag" in result.stderr
    assert not install_dir.exists()


def test_snap_docker_rejects_a_hidden_install_dir(tmp_path: Path):
    """Snap's home interface denies hidden paths under $HOME, so a snap-packaged
    docker cannot read .env in the default ~/.local/share install directory."""
    hidden_dir = tmp_path / ".local" / "share" / "hsync-server"
    result, _, recorded = _run_installer(
        tmp_path,
        "--no-start",
        snap_docker=True,
        install_dir=hidden_dir,
    )

    assert result.returncode == 1
    assert "snap" in result.stderr
    assert "hidden path" in result.stderr
    assert "--install-dir" in result.stderr
    assert not hidden_dir.exists()
    assert recorded == []


def test_snap_docker_allows_a_non_hidden_install_dir(tmp_path: Path):
    non_hidden = tmp_path / "hsync-server"
    result, _, recorded = _run_installer(
        tmp_path,
        "--no-start",
        snap_docker=True,
        install_dir=non_hidden,
    )

    assert result.returncode == 0, result.stderr
    assert "config" in recorded
