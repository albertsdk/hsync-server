"""Tests for the release installation rendezvous."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"


def _restricted_bin(tmp_path: Path) -> Path:
    """A minimal PATH dir without docker, so a real docker on the runner cannot
    leak into the "docker missing" preflight tests."""
    bin_dir = tmp_path / "restricted-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in (
        "bash",
        "sh",
        "uname",
        "readlink",
        "id",
        "date",
        "sleep",
        "mkdir",
        "chmod",
        "mv",
        "rm",
        "cp",
        "grep",
        "cat",
        "sed",
        "openssl",
    ):
        real = shutil.which(name)
        if real is not None:
            (bin_dir / name).symlink_to(real)
    return bin_dir


def _write_fake_commands(
    tmp_path: Path,
    *,
    snap_docker: bool = False,
    config_fails: bool = False,
    with_docker: bool = True,
    compose_version_fails: bool = False,
    compose_v1: bool = False,
    daemon_message: str | None = None,
    with_cosign: bool = True,
    cosign_exit: int = 0,
    cosign_version: str = "v3.1.4",
    uname_stub: tuple[str, str] | None = None,
) -> Path:
    docker_args = tmp_path / "docker.args"
    # A snap-packaged docker is exposed through /snap/bin; mirror that layout so
    # the installer's snap detection sees the same path shape.
    docker_dir = tmp_path / "snap" / "bin" if snap_docker else tmp_path
    docker_dir.mkdir(parents=True, exist_ok=True)
    if with_docker:
        docker = docker_dir / "docker"
        docker_body = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "$HSYNC_FAKE_DOCKER_ARGS"\n'
        if compose_version_fails:
            docker_body += 'if [[ "$1" == "compose" && "$2" == "version" ]]; then\n  exit 1\nfi\n'
        if daemon_message is not None:
            docker_body += (
                'if [[ "$1" == "info" ]]; then\n'
                "  printf '%s\\n' \"${HSYNC_FAKE_DOCKER_INFO:-}\" >&2\n"
                "  exit 1\n"
                "fi\n"
            )
        if config_fails:
            docker_body += (
                'if [[ "$*" == *" config "* ]]; then\n'
                "  printf 'open /.env: permission denied\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
            )
        docker.write_text(docker_body + "exit 0\n")
        docker.chmod(0o755)

    if compose_v1:
        compose_v1_bin = tmp_path / "docker-compose"
        compose_v1_bin.write_text("#!/usr/bin/env bash\nexit 0\n")
        compose_v1_bin.chmod(0o755)

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

    if with_cosign:
        cosign = tmp_path / "cosign"
        cosign.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'%s\\n\' "$@" >> "{tmp_path / "cosign.args"}"\n'
            'if [[ "$1" == "version" ]]; then\n'
            f'  printf "GitVersion:    {cosign_version}\\n"\n'
            "  exit 0\n"
            "fi\n"
            f"exit {cosign_exit}\n"
        )
        cosign.chmod(0o755)

    if uname_stub is not None:
        uname_os, uname_arch = uname_stub
        uname = tmp_path / "uname"
        uname.write_text(
            "#!/usr/bin/env bash\n"
            'case "$1" in\n'
            f'  -s) printf "%s\\n" "{uname_os}" ;;\n'
            f'  -m) printf "%s\\n" "{uname_arch}" ;;\n'
            '  *) printf "unknown\\n" ;;\n'
            "esac\n"
        )
        uname.chmod(0o755)
    return docker_args


def _run_installer(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    snap_docker: bool = False,
    install_dir: Path | None = None,
    config_fails: bool = False,
    use_default_dir: bool = False,
    with_docker: bool = True,
    compose_version_fails: bool = False,
    compose_v1: bool = False,
    daemon_message: str | None = None,
    with_cosign: bool = True,
    cosign_exit: int = 0,
    cosign_version: str = "v3.1.4",
    uname_stub: tuple[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    docker_args = _write_fake_commands(
        tmp_path,
        snap_docker=snap_docker,
        config_fails=config_fails,
        with_docker=with_docker,
        compose_version_fails=compose_version_fails,
        compose_v1=compose_v1,
        daemon_message=daemon_message,
        with_cosign=with_cosign,
        cosign_exit=cosign_exit,
        cosign_version=cosign_version,
        uname_stub=uname_stub,
    )
    if install_dir is None:
        install_dir = tmp_path / "installation"
    env = os.environ.copy()
    if with_docker:
        # Prepend both the plain and the snap-style bin dir; only one holds `docker`.
        # Without a fake cosign the tail of PATH is the restricted set, so a real
        # cosign on the runner cannot mask the "verifier missing" path.
        tail = env["PATH"] if with_cosign else str(_restricted_bin(tmp_path))
        env["PATH"] = os.pathsep.join([str(tmp_path), str(tmp_path / "snap" / "bin"), tail])
    else:
        env["PATH"] = str(_restricted_bin(tmp_path))
    env["HSYNC_FAKE_DOCKER_ARGS"] = str(docker_args)
    if daemon_message is not None:
        env["HSYNC_FAKE_DOCKER_INFO"] = daemon_message
    if extra_env:
        env.update(extra_env)
    cmd = [str(INSTALL_SCRIPT)]
    if not use_default_dir:
        cmd += ["--install-dir", str(install_dir)]
    result = subprocess.run(
        cmd + list(args),
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
    assert "bash -s -- --install-dir" in result.stderr
    assert not hidden_dir.exists()
    assert recorded == []


def test_snap_docker_adapts_a_default_hidden_install_dir(tmp_path: Path):
    """With snap docker and no explicit --install-dir, the installer falls back to
    a non-hidden default instead of failing the whole install."""
    home = tmp_path / "home"
    home.mkdir()
    result, _, recorded = _run_installer(
        tmp_path,
        "--no-start",
        snap_docker=True,
        use_default_dir=True,
        extra_env={"HOME": str(home)},
    )

    assert result.returncode == 0, result.stderr
    assert (home / "hsync-server" / ".env").exists()


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


def test_compose_failure_hints_at_snap_hidden_paths(tmp_path: Path):
    """A snap docker failure carrying the hidden-path signature gets an
    actionable hint even where the pre-flight check did not catch it."""
    non_hidden = tmp_path / "hsync-server"
    result, _, _ = _run_installer(
        tmp_path,
        "--no-start",
        snap_docker=True,
        config_fails=True,
        install_dir=non_hidden,
    )

    assert result.returncode == 1
    assert "Warning: docker is the snap package" in result.stderr
    assert "bash -s -- --install-dir" in result.stderr


def test_compose_failure_without_snap_adds_no_snap_hint(tmp_path: Path):
    result, _, _ = _run_installer(tmp_path, "--no-start", config_fails=True)

    assert result.returncode == 1
    assert "permission denied" in result.stderr
    assert "Warning: docker is the snap package" not in result.stderr


# --- Prerequisite guidance ---------------------------------------------------


def test_dry_run_works_without_docker(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--dry-run", with_docker=False)

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert not install_dir.exists()


def test_missing_docker_prints_install_guidance(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--no-start", with_docker=False)

    assert result.returncode == 1
    assert "required command not found: docker" in result.stderr
    assert "docs.docker.com" in result.stderr
    assert not install_dir.exists()


def test_docker_compose_v2_missing_prints_guidance(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--no-start", compose_version_fails=True)

    assert result.returncode == 1
    assert "Docker Compose v2 is required" in result.stderr
    assert "docs.docker.com/compose/install" in result.stderr
    assert not install_dir.exists()


def test_docker_compose_v1_only_is_called_out(tmp_path: Path):
    result, _, _ = _run_installer(
        tmp_path, "--no-start", compose_version_fails=True, compose_v1=True
    )

    assert result.returncode == 1
    assert "Compose v1" in result.stderr


def test_docker_daemon_unreachable_prints_start_guidance(tmp_path: Path):
    result, install_dir, _ = _run_installer(
        tmp_path,
        daemon_message="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )

    assert result.returncode == 1
    assert "Cannot reach the Docker daemon" in result.stderr
    assert not install_dir.exists()


def test_docker_permission_denied_prints_group_guidance(tmp_path: Path):
    result, install_dir, _ = _run_installer(
        tmp_path,
        daemon_message="permission denied while trying to connect to the Docker daemon socket",
    )

    assert result.returncode == 1
    assert "usermod -aG docker" in result.stderr
    assert not install_dir.exists()


# --- Image signature verification --------------------------------------------


def test_default_run_verifies_the_image(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "Verifying the container image signature" in result.stdout
    logged = (tmp_path / "cosign.args").read_text()
    assert "verify" in logged
    assert "ghcr.io/albertsdk/hsync-server:v1.0.0" in logged
    assert "--certificate-oidc-issuer" in logged
    assert "https://token.actions.githubusercontent.com" in logged
    assert (install_dir / ".env").exists()


def test_missing_cosign_prints_install_guidance_and_writes_nothing(tmp_path: Path):
    result, install_dir, _ = _run_installer(
        tmp_path, "--no-start", with_cosign=False, uname_stub=("Linux", "x86_64")
    )

    assert result.returncode != 0
    assert "required command not found: cosign" in result.stderr
    # The concrete command follows the host distro family (debian/rhel/suse print
    # the official binary, Arch/Alpine their package), so accept any of them.
    assert (
        "cosign-linux-amd64" in result.stderr
        or "sudo pacman -S cosign" in result.stderr
        or "sudo apk add cosign" in result.stderr
    )
    assert "bash -s -- --no-verify" in result.stderr
    # The verifier is a preflight, so the installation directory is never created.
    assert not install_dir.exists()


def test_no_verify_skips_verification(tmp_path: Path):
    result, install_dir, _ = _run_installer(
        tmp_path, "--no-start", "--no-verify", with_cosign=False
    )

    assert result.returncode == 0, result.stderr
    assert "verification is disabled" in result.stderr
    assert (install_dir / ".env").exists()


def test_failed_verification_fails_closed(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--no-start", cosign_exit=1)

    assert result.returncode != 0
    assert "signature verification failed" in result.stderr
    assert not install_dir.exists()


def test_failed_verification_with_an_old_cosign_points_at_the_upgrade(tmp_path: Path):
    result, _, _ = _run_installer(tmp_path, "--no-start", cosign_exit=1, cosign_version="v2.4.1")

    assert result.returncode != 0
    assert "cosign 2.x cannot verify the v3 bundle signature" in result.stderr


def test_image_digest_pins_the_exact_reference(tmp_path: Path):
    digest = "sha256:" + "b" * 64
    result, install_dir, _ = _run_installer(tmp_path, "--no-start", "--image-digest", digest)

    assert result.returncode == 0, result.stderr
    env_file = (install_dir / ".env").read_text()
    assert f"HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server@{digest}" in env_file
    assert f"ghcr.io/albertsdk/hsync-server@{digest}" in (tmp_path / "cosign.args").read_text()


def test_image_digest_survives_a_rerun(tmp_path: Path):
    digest = "sha256:" + "c" * 64
    first, install_dir, _ = _run_installer(tmp_path, "--no-start", "--image-digest", digest)
    second, _, _ = _run_installer(tmp_path, "--no-start", "--image-digest", digest)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (
        f"HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server@{digest}" in (install_dir / ".env").read_text()
    )


def test_invalid_digest_is_rejected_before_writing(tmp_path: Path):
    result, install_dir, _ = _run_installer(tmp_path, "--no-start", "--image-digest", "sha256:zz")

    assert result.returncode != 0
    assert "invalid --image-digest" in result.stderr
    assert not install_dir.exists()


def test_help_documents_the_verification_flags(tmp_path: Path):
    result, _, _ = _run_installer(tmp_path, "--help")

    assert result.returncode == 0
    assert "--no-verify" in result.stdout
    assert "--image-digest" in result.stdout
    assert "cosign" in result.stdout


def test_release_workflow_signs_the_image():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "id-token: write" in workflow
    assert "sigstore/cosign-installer" in workflow
    assert "cosign sign --yes" in workflow
    assert "steps.build.outputs.digest" in workflow
