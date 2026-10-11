"""Guard for the container build commands in ``../compile.sh``.

The ``build`` group (PyInstaller) must be requested with the syntax Poetry
honours. With Poetry 2.x — 2.3.2 is the version pinned in
``packaging/deb/Dockerfile.builder`` — ``--only`` prunes every group not named in
its list and a co-present ``--with`` is ignored, so
``poetry install --only main --with build`` left PyInstaller out of the
container venv and the cross-build targets (``rpi``, ``deb-all``) aborted.
A white-box guard on the script text is used because exercising the real
cross-build needs Docker, qemu and network access.

Consumers: hsync-server maintainers (regression guard).
"""

from __future__ import annotations

import re
from pathlib import Path

COMPILE_SH = Path(__file__).resolve().parents[1] / "compile.sh"

# The historical regression: Poetry 2.x ignores `--with` next to `--only`.
_BROKEN = "poetry install --only main --with build"
_FIXED = "poetry install --only main,build"


def _script() -> str:
    return COMPILE_SH.read_text(encoding="utf-8")


def _poetry_install_lines() -> list[str]:
    return [line.strip() for line in _script().splitlines() if "poetry install" in line]


def _build_group_requested(line: str) -> bool:
    """True when ``line`` installs the ``build`` group under Poetry 2.x rules."""
    only = re.search(r"--only\s+([^\s;]+)", line)
    if only:
        # Inside --only the group list is exhaustive: --with cannot re-add it.
        return "build" in only.group(1).split(",")
    return "--without build" not in line and "--without=build" not in line


def test_guard_rejects_the_pruned_build_group() -> None:
    """The guard must fail on the pre-fix form and pass on the fixed one."""
    assert not _build_group_requested(_BROKEN)
    assert _build_group_requested(_FIXED)
    # The `;`-terminated form used inside the container retry loop.
    assert not _build_group_requested("if poetry install --only main --with build; then :; fi")
    assert _build_group_requested("if poetry install --only main,build; then :; fi")


def test_every_container_build_installs_the_build_group() -> None:
    lines = _poetry_install_lines()
    assert lines, "no 'poetry install' invocation found in compile.sh"
    for line in lines:
        assert _build_group_requested(line), f"build group not installed: {line}"


def test_container_workdir_is_the_poetry_project_root() -> None:
    """The relative artifact paths need the project to sit at the repo root.

    hsync-bootnode hit the opposite case: its Poetry project lives in ``python/``,
    so the container workdir was one level below the repository root and the
    relative ``build/deb-all/...`` distpath landed in ``python/build/``. If this
    project ever moves into a subdirectory, the container workdir must be
    remounted and the artifact paths made absolute.
    """
    assert "-w /repo/hsync-server" in _script()
    assert (COMPILE_SH.parent / "pyproject.toml").is_file()


def test_rpm_version_mapping_is_registered() -> None:
    """RPM >= 4.10 uses ``~`` for "sorts before everything", so the SemVer -> RPM
    Version: mapping is the same as Debian; ``_semver_to_rpm`` must delegate to
    ``_semver_to_debian`` rather than re-encode the rule."""
    script = _script()
    assert "_semver_to_rpm()" in script
    assert '_semver_to_debian "$1"' in script


def test_rpm_arch_mapping_is_64_bit_only() -> None:
    """RHEL/Rocky is 64-bit only: only x86_64 and aarch64 are mapped."""
    script = _script()
    assert 'x86_64|amd64)   echo "x86_64"' in script
    assert 'aarch64|arm64)  echo "aarch64"' in script


def test_rpm_targets_are_registered_in_dispatch() -> None:
    """``rpm`` and ``rpm-all`` must be wired into main() and help."""
    script = _script()
    assert "rpm)" in script
    assert "rpm-all)" in script
    assert "Supported targets:" in script
    assert "rpm-all" in script


def test_docs_latest_download_names_are_created_by_the_release() -> None:
    """Every fixed-name asset the docs download from releases/latest/download must
    be created as an alias by the release workflow, so the link cannot 404. Names
    with the VERSION/ARCH placeholders are pinned examples, not aliases."""
    repo = COMPILE_SH.parent
    workflow = (repo / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    names: set[str] = set()
    for md in (repo / "docs").rglob("*.md"):
        for name in re.findall(
            r"releases/latest/download/([A-Za-z0-9._-]+)", md.read_text(encoding="utf-8")
        ):
            if name.startswith(repo.name) and "VERSION" not in name and "ARCH" not in name:
                names.add(name)
    names -= {"install.sh", "SHA256SUMS"}
    assert names, "expected at least one fixed latest/download asset in the docs"
    # The fixed-name targets are the `dst` half of the `src|dst` alias specs.
    aliases = set(re.findall(r'\|([A-Za-z0-9._-]+)"', workflow))
    missing = sorted(n for n in names if n not in aliases)
    assert not missing, f"docs reference latest aliases the workflow does not create: {missing}"


def test_pyinstaller_lookup_is_windows_aware() -> None:
    """Windows venvs put console scripts under ``Scripts/`` (with a ``.exe``
    suffix), not ``bin/``. A hardcoded ``bin/`` path makes the native Windows
    build abort with "PyInstaller not found" although Poetry installed it
    (the build runs ``poetry install`` then ``./compile.sh`` without activating
    the venv, so the PATH fallback misses too).
    """
    script = _script()
    assert 'if [[ "${OS_NAME}" == "windows" ]]; then' in script
    assert 'PYINSTALLER="${VENV_DIR}/Scripts/pyinstaller.exe"' in script
    assert 'PYINSTALLER="${VENV_DIR}/bin/pyinstaller"' in script


def test_run_script_console_path_is_windows_aware() -> None:
    """``run.sh`` must find the console script under ``Scripts/`` on Windows."""
    script = (COMPILE_SH.parent / "run.sh").read_text(encoding="utf-8")
    assert 'VENV_BIN_DIR="Scripts"' in script
    assert 'BIN_EXT=".exe"' in script
    assert 'readonly BIN="${SCRIPT_DIR}/.venv/${VENV_BIN_DIR}/hsync-server${BIN_EXT}"' in script
