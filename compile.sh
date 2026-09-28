#!/usr/bin/env bash
#
# HSYNC Server - Build Script
#
# Compiles the HSYNC server into a standalone binary using PyInstaller.
# The resulting binary is self-contained and doesn't require Python.
#
# Usage:
#   ./compile.sh [options]
#
# Options:
#   -c, --clean             Clean build artifacts before building
#   -t, --target TARGET     Build target (default: native)
#                           Supported:
#                               native               host OS / arch standalone
#                                                    executable
#                               deb                  Debian/Ubuntu .deb package
#                                                    for the host arch
#                               deb-all              .deb for amd64, arm64 and
#                                                    armhf (via Docker)
#                               rpi                  Standalone executables for
#                                                    Raspberry Pi OS 32-bit
#                                                    (armhf) and 64-bit (arm64)
#                                                    via Docker
#   -v, --version VERSION   Override the version used for packages (default:
#                           the pyproject.toml version). Also settable via the
#                           HSYNC_VERSION environment variable. A prerelease
#                           (e.g. 1.2.0-alpha.1) is translated to the Debian
#                           form (1.2.0~alpha.1) for the .deb target.
#   -h, --help              Show this help message
#
# Requirements:
#   - native target: Python virtual environment (.venv) with the build group
#     installed; run 'poetry install --with build'
#   - deb target: Python virtual environment (.venv) + dpkg-deb (available on
#     any Debian/Ubuntu system via dpkg-dev)
#   - deb-all / rpi targets: Docker with qemu-user-static (binfmt) configured
#     (used for cross-arch emulation on amd64 hosts). Dependencies are
#     installed inside the container, so the host virtualenv is not used.
#
# Output:
#   native:
#     - dist/<os>-<arch>/hsync-server[.exe]   Standalone platform-aware
#       executable, e.g. dist/linux-amd64/hsync-server
#   deb:
#     - dist/deb/hsync-server_<ver>_<arch>.deb   Installable with dpkg -i
#   deb-all:
#     - dist/deb/hsync-server_<ver>_amd64.deb
#     - dist/deb/hsync-server_<ver>_arm64.deb
#     - dist/deb/hsync-server_<ver>_armhf.deb
#   rpi:
#     - dist/rpi/armhf/hsync-server   Raspberry Pi OS 32-bit (armv7)
#     - dist/rpi/arm64/hsync-server   Raspberry Pi OS 64-bit (aarch64)
#
# Author: Alberto Basso
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Script directory (resolves symlinks)
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Platform detection (OS + architecture)
# Normalize OS name: linux | darwin | windows
case "$(uname -s)" in
    Linux*)               readonly OS_NAME="linux" ;;
    Darwin*)              readonly OS_NAME="darwin" ;;
    MINGW*|CYGWIN*|MSYS*) readonly OS_NAME="windows" ;;
    *)                    readonly OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac

# Normalize architecture: amd64 | arm64 | armv7 | x86 | ...
case "$(uname -m)" in
    x86_64|amd64)   readonly ARCH_NAME="amd64" ;;
    aarch64|arm64)  readonly ARCH_NAME="arm64" ;;
    armv7l|armv7)   readonly ARCH_NAME="armv7" ;;
    i386|i686)      readonly ARCH_NAME="x86" ;;
    *)              readonly ARCH_NAME="$(uname -m)" ;;
esac

readonly PLATFORM="${OS_NAME}-${ARCH_NAME}"

# Build configuration
readonly APP_NAME="hsync-server"
readonly ENTRY_POINT="hsync_server/__main__.py"
readonly PYPROJECT="pyproject.toml"
readonly BIN_EXT="$([[ "${OS_NAME}" == "windows" ]] && echo ".exe" || echo "")"
readonly OUTPUT_BASE="dist"
readonly OUTPUT_DIR="${OUTPUT_BASE}/${PLATFORM}"
readonly BUILD_DIR="build"
readonly VENV_DIR=".venv"
# PyInstaller lives under Scripts/ on Windows venvs and bin/ elsewhere.
if [[ "${OS_NAME}" == "windows" ]]; then
    readonly PYINSTALLER="${VENV_DIR}/Scripts/pyinstaller.exe"
else
    readonly PYINSTALLER="${VENV_DIR}/bin/pyinstaller"
fi
# --strip needs binutils and is unreliable on Windows; omit it there.
readonly STRIP_FLAG="$([[ "${OS_NAME}" == "windows" ]] && echo "" || echo "--strip")"

# -----------------------------------------------------------------------------
# Raspberry Pi OS (armhf + arm64) — standalone executables via Docker
# -----------------------------------------------------------------------------

# The Raspberry Pi cross-build image carries Python 3.12 + Poetry and the
# armhf source-build toolchain (Rust, libssl-dev, …) required by the project's
# native dependencies. Raspberry Pi OS Bookworm is Debian 12 (glibc 2.36),
# matching the bookworm base images.
readonly RPI_OUTPUT_DIR="${OUTPUT_BASE}/rpi"
readonly RPI_DOCKERFILE="packaging/rpi/Dockerfile.builder"
readonly RPI_BUILDER_IMAGE_PREFIX="hsync-server-rpi-builder"

# -----------------------------------------------------------------------------
# Colors (disabled if not a terminal)
# -----------------------------------------------------------------------------

if [[ -t 1 ]]; then
    # ANSI-C quoting ($'...') so the variables hold the actual ESC byte and
    # render correctly with plain ``printf``/``cat`` as well as ``echo -e``.
    readonly RED=$'\033[0;31m'
    readonly GREEN=$'\033[0;32m'
    readonly YELLOW=$'\033[0;33m'
    readonly BLUE=$'\033[0;34m'
    readonly CYAN=$'\033[0;36m'
    readonly BOLD=$'\033[1m'
    readonly DIM=$'\033[2m'
    readonly RESET=$'\033[0m'
else
    readonly RED=''
    readonly GREEN=''
    readonly YELLOW=''
    readonly BLUE=''
    readonly CYAN=''
    readonly BOLD=''
    readonly DIM=''
    readonly RESET=''
fi

# -----------------------------------------------------------------------------
# Logging Functions
# -----------------------------------------------------------------------------

# Log informational message
log_info() { echo -e "${BLUE}ℹ${RESET} $*"; }

# Log success message
log_ok() { echo -e "${GREEN}✓${RESET} $*"; }

# Log warning message
log_warn() { echo -e "${YELLOW}⚠${RESET} $*"; }

# Log error message to stderr
log_error() { echo -e "${RED}✕${RESET} $*" >&2; }

# Log step/action message
log_step() { echo -e "${CYAN}→${RESET} ${BOLD}$*${RESET}"; }

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

# Report whether a binfmt_misc handler exists and is enabled.
# $1 = handler name (e.g. qemu-arm, qemu-aarch64)
qemu_handler_active() {
    [[ -d "/proc/sys/fs/binfmt_misc" ]] || return 1
    local f="/proc/sys/fs/binfmt_misc/$1"
    [[ -f "${f}" ]] && grep -q '^enabled' "${f}"
}

# Ensure the qemu-user-static binfmt handlers required for cross-arch Docker
# builds are registered and active. $@ = required handler names.
ensure_qemu_binfmt() {
    local handlers=("$@")
    [[ ${#handlers[@]} -eq 0 ]] && handlers=("qemu-arm")
    log_step "Ensuring qemu-user-static binfmt handlers are active: ${handlers[*]}"
    local missing=()
    local h
    for h in "${handlers[@]}"; do
        qemu_handler_active "${h}" || missing+=("${h}")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log_ok "binfmt handlers already active"
        return 0
    fi
    log_info "Registering qemu-user-static binfmt handlers..."
    # Image pinned by digest: this runs --privileged, so it must not move.
    if ! docker run --rm --privileged \
        multiarch/qemu-user-static@sha256:fe60359c92e86a43cc87b3d906006245f77bfc0565676b80004cc666e4feb9f0 \
        --reset -p yes >/dev/null 2>&1; then
        log_error "Failed to run multiarch/qemu-user-static (is the Docker daemon reachable?)."
        exit 1
    fi
    missing=()
    for h in "${handlers[@]}"; do
        qemu_handler_active "${h}" || missing+=("${h}")
    done
    if [[ ${#missing[@]} -ne 0 ]]; then
        log_error "binfmt handlers still inactive after registration: ${missing[*]}"
        log_error "Cross-arch emulation is unavailable; aborting now to avoid a"
        log_error "misleading 'exec format error' later in the build."
        exit 1
    fi
    log_ok "binfmt handlers registered and active"
}

# Display usage information
show_help() {
    cat << EOF
${BOLD}HSYNC Server - Build Script${RESET}

${BOLD}Usage:${RESET}
    ./compile.sh [options]

${BOLD}Options:${RESET}
    -c, --clean             Clean build artifacts before building
    -t, --target TARGET     Build target (default: native)
                            Supported: native, deb, deb-all, rpi
    -v, --version VERSION   Override the version used for packages (default:
                            pyproject.toml). Also settable via HSYNC_VERSION.
                            The frozen binary's own reported version is fixed
                            at build time (hsync_server.constants),
                            independent of this value.
    -h, --help              Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh                        # Native host build
    ./compile.sh --clean                # Clean and rebuild
    ./compile.sh --target deb           # .deb for host arch
    ./compile.sh --target deb-all       # .deb for amd64, arm64, armhf
    ./compile.sh --target rpi           # Raspberry Pi armhf + arm64

${BOLD}Output:${RESET}
    native:
        ${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}   Standalone executable (platform: ${PLATFORM})
    deb:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_<arch>.deb
    deb-all:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_amd64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_arm64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_armhf.deb
    rpi:
        dist/rpi/armhf/${APP_NAME}   Raspberry Pi OS 32-bit (armv7)
        dist/rpi/arm64/${APP_NAME}   Raspberry Pi OS 64-bit (aarch64)
EOF
}

# Clean build artifacts
clean_artifacts() {
    log_step "Cleaning build artifacts..."

    local cleaned=0

    # Native platform output.
    if [[ -d "${OUTPUT_DIR}" ]]; then
        rm -rf "${OUTPUT_DIR}"
        ((cleaned++)) || true
    fi

    # Raspberry Pi output.
    if [[ -d "${RPI_OUTPUT_DIR}" ]]; then
        rm -rf "${RPI_OUTPUT_DIR}"
        ((cleaned++)) || true
    fi

    # PyInstaller intermediate files.
    if [[ -d "${BUILD_DIR}" ]]; then
        rm -rf "${BUILD_DIR}"
        ((cleaned++)) || true
    fi

    if [[ -f "${APP_NAME}.spec" ]]; then
        rm -f "${APP_NAME}.spec"
        ((cleaned++)) || true
    fi

    if [[ ${cleaned} -gt 0 ]]; then
        log_ok "Cleaned ${cleaned} artifact(s)"
    else
        log_info "No artifacts to clean"
    fi
}

# Check prerequisites
check_prerequisites() {
    # Ensure we're in the correct directory
    if [[ ! -f "${PYPROJECT}" ]]; then
        log_error "${PYPROJECT} not found. Run this script from the project root."
        exit 1
    fi

    # Check for virtual environment
    if [[ ! -d "${VENV_DIR}" ]]; then
        log_error "Virtual environment not found."
        log_info "Run 'poetry install --with build' to create it."
        exit 1
    fi

    # Check entry point exists
    if [[ ! -f "${ENTRY_POINT}" ]]; then
        log_error "Entry point not found: ${ENTRY_POINT}"
        exit 1
    fi
}

# Version override, set by --version in main() and/or the HSYNC_VERSION
# environment variable. Mutable (not readonly) so main() can assign it.
VERSION_OVERRIDE=""

# Get the version: --version flag > HSYNC_VERSION env > pyproject.toml.
get_version() {
    if [[ -n "${VERSION_OVERRIDE}" ]]; then
        printf '%s' "${VERSION_OVERRIDE}"
    elif [[ -n "${HSYNC_VERSION:-}" ]]; then
        printf '%s' "${HSYNC_VERSION}"
    else
        grep -E '^version\s*=' "${PYPROJECT}" | head -1 | sed 's/.*"\(.*\)".*/\1/'
    fi
}

# Translate a SemVer version into a Debian policy version so a prerelease sorts
# *before* the stable release. Debian uses '~' as the "sorts before everything"
# separator while '-' denotes the debian revision, so the first '-' (the SemVer
# prerelease separator) becomes '~': 1.2.0-alpha.1 -> 1.2.0~alpha.1.
_semver_to_debian() {
    printf '%s' "$1" | sed 's/-/~/'
}

# Map uname -m to Debian architecture name.
_arch_to_deb() {
    case "$1" in
        x86_64|amd64)   echo "amd64" ;;
        aarch64|arm64)  echo "arm64" ;;
        armv7l|armv7)   echo "armhf" ;;
        *)              echo "$1" ;;
    esac
}

# PyInstaller flags shared by the native (host) and rpi (container) onefile
# builds. --strip is added by the caller: omitted on Windows hosts, always used
# inside the Linux build containers. --distpath is also added by the caller.
# Backslash continuations (not embedded newlines) keep the expansion on a
# single line so it is safe to interpolate inside a ``bash -c`` string.
_onefile_pyi_flags() {
    printf '%s' "--clean --onefile --noconfirm \
        --name ${APP_NAME} \
        --log-level ERROR \
        --collect-submodules hsync_server \
        --collect-submodules hsync_common \
        --hidden-import uvicorn.logging \
        --hidden-import uvicorn.loops.auto \
        --hidden-import uvicorn.protocols.http.auto \
        --hidden-import uvicorn.protocols.websockets.auto \
        --hidden-import uvicorn.lifespan.on \
        --hidden-import websockets.legacy \
        --hidden-import websockets.legacy.server \
        --hidden-import rich._unicode_data \
        --hidden-import rich._unicode_data.unicode17-0-0 \
        --hidden-import rich._unicode_data.unicode16-0-0 \
        --hidden-import rich._unicode_data.unicode15-1-0 \
        --hidden-import rich._unicode_data.unicode15-0-0 \
        --hidden-import rich._unicode_data.unicode14-0-0 \
        --hidden-import rich._unicode_data._versions \
        --exclude-module pytest --exclude-module pytest_asyncio \
        --exclude-module pytest_cov --exclude-module tkinter \
        --exclude-module unittest --exclude-module pdb \
        --exclude-module doctest --exclude-module pip \
        --exclude-module setuptools --exclude-module wheel \
        --exclude-module pkg_resources"
}

# PyInstaller flags for the --onedir build used by the deb/deb-all targets.
# Same hidden imports as _onefile_pyi_flags, minus --onefile (the onedir
# layout is staged under /usr/lib/hsync-server inside the package).
_onedir_pyi_flags() {
    printf '%s' "--clean --onedir --noconfirm \
        --name ${APP_NAME} \
        --log-level ERROR \
        --collect-submodules hsync_server \
        --collect-submodules hsync_common \
        --hidden-import uvicorn.logging \
        --hidden-import uvicorn.loops.auto \
        --hidden-import uvicorn.protocols.http.auto \
        --hidden-import uvicorn.protocols.websockets.auto \
        --hidden-import uvicorn.lifespan.on \
        --hidden-import websockets.legacy \
        --hidden-import websockets.legacy.server \
        --hidden-import rich._unicode_data \
        --hidden-import rich._unicode_data.unicode17-0-0 \
        --hidden-import rich._unicode_data.unicode16-0-0 \
        --hidden-import rich._unicode_data.unicode15-1-0 \
        --hidden-import rich._unicode_data.unicode15-0-0 \
        --hidden-import rich._unicode_data.unicode14-0-0 \
        --hidden-import rich._unicode_data._versions \
        --exclude-module pytest --exclude-module pytest_asyncio \
        --exclude-module pytest_cov --exclude-module tkinter \
        --exclude-module unittest --exclude-module pdb \
        --exclude-module doctest --exclude-module pip \
        --exclude-module setuptools --exclude-module wheel \
        --exclude-module pkg_resources"
}

# Clean up temporary files after a successful native build
cleanup_temp_files() {
    log_step "Cleaning up temporary files..."

    local cleaned=0

    # Remove build directory (PyInstaller intermediate files)
    if [[ -d "${BUILD_DIR}" ]]; then
        rm -rf "${BUILD_DIR}"
        ((cleaned++)) || true
    fi

    # Remove auto-generated spec file
    if [[ -f "${APP_NAME}.spec" ]]; then
        rm -f "${APP_NAME}.spec"
        ((cleaned++)) || true
    fi

    if [[ ${cleaned} -gt 0 ]]; then
        log_ok "Removed ${cleaned} temporary file(s)"
    fi
}

# Build the standalone binary for the host OS/arch.
# Output: dist/<os>-<arch>/hsync-server[.exe]
build_native() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for ${PLATFORM}..."

    # Verify PyInstaller is available
    if [[ ! -x "${PYINSTALLER}" ]]; then
        log_error "PyInstaller not found at ${PYINSTALLER}"
        log_error "Run 'poetry install --with build' first to install dependencies"
        exit 1
    fi

    mkdir -p "${OUTPUT_DIR}"

    # --strip: remove debug symbols to reduce binary size.
    # LC_ALL=C: force the English locale to avoid ldconfig localization
    # warnings. PYTHONWARNINGS=ignore: suppress build-time Python warnings.
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --distpath "${OUTPUT_DIR}" \
        ${STRIP_FLAG} \
        $(_onefile_pyi_flags) \
        "${ENTRY_POINT}"

    # Verify output
    local bin_path="${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}"
    if [[ -f "${bin_path}" ]]; then
        local size
        size="$(du -h "${bin_path}" | cut -f1)"

        # Clean up temporary build files
        cleanup_temp_files

        echo
        log_ok "Build successful!"
        echo
        echo -e "  ${BOLD}Binary:${RESET}   ${bin_path}"
        echo -e "  ${BOLD}Platform:${RESET} ${PLATFORM}"
        echo -e "  ${BOLD}Size:${RESET}     ${size}"
        echo -e "  ${BOLD}Version:${RESET}  ${version}"
        echo
        echo -e "${DIM}Run with: ./${bin_path} --version${RESET}"
    else
        log_error "Build failed: output binary not found at ${bin_path}"
        exit 1
    fi
}

# Build standalone executables for Raspberry Pi OS, one per supported
# architecture: armhf (32-bit, armv7) and arm64 (64-bit, aarch64). PyInstaller
# cannot cross-compile, so each binary is produced by running it inside an
# emulated python:3.12-bookworm container for the target CPU family.
#
# Output: dist/rpi/<arch>/hsync-server
build_rpi() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for Raspberry Pi OS (armhf, arm64)..."

    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the rpi target."
        exit 1
    fi
    if [[ ! -f "${RPI_DOCKERFILE}" ]]; then
        log_error "Builder Dockerfile missing: ${RPI_DOCKERFILE}"
        exit 1
    fi

    # Monorepo root: mounts hsync-server AND hsync-common (the Poetry path
    # dependency) into the container at /repo.
    local repo_root
    repo_root="$(cd "${SCRIPT_DIR}/.." && pwd)"

    ensure_qemu_binfmt qemu-arm qemu-aarch64

    local -A arch_docker_platform
    arch_docker_platform["armhf"]="linux/arm/v7"
    arch_docker_platform["arm64"]="linux/arm64"

    local -a rpi_archs=("armhf" "arm64")
    local deb_arch
    for deb_arch in "${rpi_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${deb_arch}]}"
        local builder_tag="${RPI_BUILDER_IMAGE_PREFIX}:${deb_arch}"
        local out_dir="${RPI_OUTPUT_DIR}/${deb_arch}"
        local out_abs
        mkdir -p "${out_dir}"
        out_abs="$(cd "${out_dir}" && pwd)"

        echo
        log_step "─── ${deb_arch} build ───────────────────────────────────────"

        log_step "Building Docker image ${builder_tag}..."
        DOCKER_BUILDKIT=1 docker build \
            --platform "${docker_platform}" \
            --build-arg TARGET_PLATFORM="${docker_platform}" \
            -t "${builder_tag}" \
            -f "${RPI_DOCKERFILE}" \
            "${RPI_DOCKERFILE%/*}"

        # The container installs its dependencies into a container-local venv
        # (HOME=/tmp) so the mounted source tree stays pristine; only the
        # produced executable is written back to the host under /out.
        # HSYNC_PREFER_PATH_PYINSTALLER keeps the container's own (target-arch)
        # PyInstaller from being shadowed by the mounted host .venv.
        log_step "Compiling ${deb_arch} binary inside container..."
        docker run --rm \
            --platform "${docker_platform}" \
            -u "$(id -u):$(id -g)" \
            -e HOME=/tmp -e XDG_CACHE_HOME=/tmp \
            -v "${repo_root}:/repo" \
            -v "${out_abs}:/out" \
            -w /repo/hsync-server \
            -e LC_ALL=C -e PYTHONWARNINGS=ignore \
            -e PIP_DEFAULT_TIMEOUT=300 -e PIP_RETRIES=10 \
            -e POETRY_REQUESTS_TIMEOUT=300 \
            -e HSYNC_VERSION="${version}" \
            "${builder_tag}" \
            bash -c "
                set -euo pipefail
                poetry install --only main --with build
                HSYNC_PREFER_PATH_PYINSTALLER=1 poetry run pyinstaller --strip \
                    $(_onefile_pyi_flags) \
                    --distpath /out \
                    --workpath /tmp/work \
                    --specpath /tmp/work \
                    ${ENTRY_POINT}
            "

        local bin_path="${out_dir}/${APP_NAME}"
        if [[ ! -f "${bin_path}" ]]; then
            log_error "${deb_arch} binary not produced at ${bin_path}"
            exit 1
        fi
        log_ok "  → ${RPI_OUTPUT_DIR}/${deb_arch}/${APP_NAME} ($(du -h "${bin_path}" | cut -f1))"
    done

    echo
    log_ok "Raspberry Pi executables built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}  ${version}"
    echo -e "  ${BOLD}Output:${RESET}   ${RPI_OUTPUT_DIR}/<arch>/${APP_NAME}"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  Raspberry Pi executables:${RESET}"
    for deb_arch in "${rpi_archs[@]}"; do
        echo -e "${GREEN}${BOLD}      $(pwd)/${RPI_OUTPUT_DIR}/${deb_arch}/${APP_NAME}${RESET}"
    done
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
}

# -----------------------------------------------------------------------------
# Debian/Ubuntu .deb package build
# -----------------------------------------------------------------------------

# Configuration for the deb target.
readonly DEB_DIR="packaging/deb"
readonly DEB_SRC_CONTROL="${DEB_DIR}/DEBIAN"
readonly DEB_SRC_DATA="${DEB_DIR}"
readonly DEB_PKG_NAME="hsync-server"
readonly DEB_OUTPUT_DIR="${OUTPUT_BASE}/deb"
readonly DEB_BUILD_DIR="${BUILD_DIR}/deb"

# Normalise a staged .deb payload so the resulting package is lintian-clean:
#   * directories 0755 / files 0644 under etc/ and usr/;
#   * the main executable keeps its 0755 bit;
#   * the man page is gzip-compressed (-n => reproducible header).
# Shared between the native (build_deb) and Docker (build_deb_all) staging.
_finalize_deb_stage() {
    local stage_dir="$1"
    find "${stage_dir}/etc" "${stage_dir}/usr" -type d -exec chmod 0755 {} + 2>/dev/null || true
    find "${stage_dir}/etc" "${stage_dir}/usr" -type f -exec chmod 0644 {} + 2>/dev/null || true
    chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"
    local man="${stage_dir}/usr/share/man/man1/${APP_NAME}.1"
    [[ -f "${man}" ]] && gzip -n -9 -f "${man}"
    # Sensitive configuration (HSYNC_ADMIN_KEY): restrict to root only so the
    # shipped package is never world-readable. postinst re-asserts ownership.
    chmod 0750 "${stage_dir}/etc/hsync-server" 2>/dev/null || true
    [[ -f "${stage_dir}/etc/hsync-server/hsync-server.conf" ]] && chmod 0600 "${stage_dir}/etc/hsync-server/hsync-server.conf"
}

# Compute the shared-library dependency string for the staged main executable
# using dpkg-shlibdeps, so the .deb carries the correct, arch-specific glibc
# (libc6) minimum version. Echoes the dependency list without the
# "shlibs:Depends=" prefix, or an empty string if dpkg-shlibdeps is unavailable
# or resolves nothing. dpkg-shlibdeps must run in the target architecture's
# dpkg environment: on the host for build_deb, in the container for deb-all.
# $1 = stage_dir
_compute_shlib_depends() {
    local stage_dir="$1"
    local bin="${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"
    command -v dpkg-shlibdeps >/dev/null 2>&1 || { echo ""; return 0; }
    [[ -f "${bin}" ]] || { echo ""; return 0; }
    # dpkg-shlibdeps must run in the target dpkg env; resolve the binary to an
    # absolute path so it still exists after we cd into the temporary dir.
    local bin_abs
    bin_abs="$(cd "$(dirname "${bin}")" && pwd)/${APP_NAME}"
    local tmp deps
    tmp="$(mktemp -d)"
    mkdir -p "${tmp}/debian"
    printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' \
        "${DEB_PKG_NAME}" "${DEB_PKG_NAME}" > "${tmp}/debian/control"
    deps="$(cd "${tmp}" && dpkg-shlibdeps -O --ignore-missing-info \
        "${bin_abs}" 2>/dev/null \
        | sed -n 's/^shlibs:Depends=//p')"
    rm -rf "${tmp}"
    echo "${deps}"
}

# Append the computed shared-library dependencies to the Depends: line of the
# staged DEBIAN/control. No-op (with a warning) if none could be computed.
# $1 = stage_dir
_inject_shlib_depends() {
    local stage_dir="$1"
    local deps
    deps="$(_compute_shlib_depends "${stage_dir}")"
    if [[ -z "${deps}" ]]; then
        log_warn "dpkg-shlibdeps unavailable; libc6 dependency not added to control"
        return 0
    fi
    log_step "Adding shared-library dependencies: ${deps}"
    sed -i.bak "s/^Depends: \(.*\)$/Depends: \1, ${deps}/" "${stage_dir}/DEBIAN/control"
    rm -f "${stage_dir}/DEBIAN/control.bak"
}

# Build a .deb package for the host architecture (native build, no Docker).
build_deb() {
    local version deb_version deb_arch
    version="$(get_version)"
    deb_version="$(_semver_to_debian "${version}")"
    deb_arch="$(_arch_to_deb "${ARCH_NAME}")"

    log_step "Building ${DEB_PKG_NAME} v${version} for ${deb_arch} (host build)..."

    # --- prerequisites ---------------------------------------------------
    check_prerequisites
    if ! command -v dpkg-deb >/dev/null 2>&1; then
        log_error "dpkg-deb is required for the deb target."
        log_info "Install it with: apt install dpkg-dev"
        exit 1
    fi
    mkdir -p "${DEB_OUTPUT_DIR}" "${DEB_BUILD_DIR}"

    # --- PyInstaller --onedir on host ------------------------------------
    local onedir_dist="${DEB_BUILD_DIR}/pyinstaller"
    mkdir -p "${onedir_dist}"

    log_step "Compiling ${APP_NAME} with PyInstaller (--onedir)..."
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --distpath "${onedir_dist}" \
        --workpath "${DEB_BUILD_DIR}/work" \
        --specpath "${DEB_BUILD_DIR}/work" \
        ${STRIP_FLAG} \
        $(_onedir_pyi_flags) \
        "${ENTRY_POINT}"

    local built_dir="${onedir_dist}/${APP_NAME}"
    if [[ ! -d "${built_dir}" ]]; then
        log_error "PyInstaller --onedir output not found at ${built_dir}"
        exit 1
    fi

    # --- stage .deb tree -------------------------------------------------
    local stage_dir="${DEB_BUILD_DIR}/${DEB_PKG_NAME}_${deb_version}_${deb_arch}"
    rm -rf "${stage_dir}"
    mkdir -p "${stage_dir}"

    log_step "Staging .deb tree at ${stage_dir}..."

    cp -a "${DEB_SRC_CONTROL}" "${stage_dir}/"
    chmod 0755 "${stage_dir}/DEBIAN"/*inst* "${stage_dir}/DEBIAN"/*rm* 2>/dev/null || true

    local item
    for item in "${DEB_SRC_DATA}"/*; do
        local bn
        bn="$(basename "${item}")"
        if [[ "${bn}" == "DEBIAN" || "${bn}" == "Dockerfile.builder" ]]; then
            continue
        fi
        cp -a "${item}" "${stage_dir}/"
    done

    # Place the PyInstaller --onedir output under /usr/lib/hsync-server/.
    mkdir -p "${stage_dir}/usr/lib"
    cp -a "${built_dir}" "${stage_dir}/usr/lib/${APP_NAME}"
    chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"

    # Create a relative symlink: /usr/bin/hsync-server -> ../lib/hsync-server/hsync-server
    mkdir -p "${stage_dir}/usr/bin"
    ln -sf "../lib/${APP_NAME}/${APP_NAME}" "${stage_dir}/usr/bin/${APP_NAME}"

    _finalize_deb_stage "${stage_dir}"

    log_step "Injecting version ${deb_version} and arch ${deb_arch} into control..."
    sed -i.bak \
        -e "s/__VERSION__/${deb_version}/g" \
        -e "s/__ARCH__/${deb_arch}/g" \
        "${stage_dir}/DEBIAN/control"
    rm -f "${stage_dir}/DEBIAN/control.bak"

    _inject_shlib_depends "${stage_dir}"

    log_step "Building .deb package..."
    local deb_cmd
    if command -v fakeroot >/dev/null 2>&1; then
        deb_cmd="fakeroot"
    else
        deb_cmd=""
        log_warn "fakeroot not available — building .deb with current user ownership"
    fi
    ${deb_cmd} dpkg-deb --build "${stage_dir}" "${DEB_OUTPUT_DIR}"

    local deb_name="${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb"
    local deb_path="${DEB_OUTPUT_DIR}/${deb_name}"
    local size deb_abs
    size="$(du -h "${deb_path}" | cut -f1)"
    deb_abs="$(cd "$(dirname "${deb_path}")" && pwd)/$(basename "${deb_path}")"

    rm -rf "${DEB_BUILD_DIR}/work" "${DEB_BUILD_DIR}/pyinstaller" "${APP_NAME}.spec" 2>/dev/null || true

    echo
    log_ok "Debian package built!"
    echo
    echo -e "  ${BOLD}Package:${RESET}  ${deb_name}"
    echo -e "  ${BOLD}Arch:${RESET}     ${deb_arch}"
    echo -e "  ${BOLD}Size:${RESET}     ${size}"
    echo -e "  ${BOLD}Version:${RESET}  ${deb_version}"
    echo
    echo -e "${DIM}Install with:${RESET}"
    echo -e "${DIM}  sudo dpkg -i ${deb_abs}${RESET}"
    echo -e "${DIM}  sudo apt install -f  # to resolve any missing dependencies${RESET}"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation package:${RESET}"
    echo -e "${GREEN}${BOLD}      ${deb_abs}${RESET}"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
}

# -----------------------------------------------------------------------------
# Debian/Ubuntu .deb — All architectures (via Docker)
# -----------------------------------------------------------------------------

# Configuration for the deb-all target.
readonly DEB_ALL_DIR="${DEB_DIR}"
readonly DEB_ALL_OUTPUT_DIR="${OUTPUT_BASE}/deb"
readonly DEB_ALL_BUILD_DIR="${BUILD_DIR}/deb-all"
readonly DEB_ALL_BUILDER_IMAGE_PREFIX="hsync-server-deb-builder"

# Build .deb packages for all 3 Debian/Ubuntu architectures (amd64, arm64, armhf)
# using Docker cross-compilation (one build per arch).
build_deb_all() {
    local version deb_version
    version="$(get_version)"
    deb_version="$(_semver_to_debian "${version}")"

    log_step "Building ${DEB_PKG_NAME} v${version} for all 3 Debian/Ubuntu architectures..."

    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the deb-all target."
        exit 1
    fi
    if [[ ! -d "${DEB_SRC_CONTROL}" ]]; then
        log_error "DEBIAN control directory missing: ${DEB_SRC_CONTROL}"
        exit 1
    fi

    mkdir -p "${DEB_ALL_OUTPUT_DIR}"

    local repo_root
    repo_root="$(cd "${SCRIPT_DIR}/.." && pwd)"

    ensure_qemu_binfmt qemu-aarch64 qemu-arm

    # Common PyInstaller flags (single-line expansion, see _onedir_pyi_flags).
    local pyinstaller_flags="$(_onedir_pyi_flags) --strip ${ENTRY_POINT}"

    local -A arch_docker_platform
    arch_docker_platform["amd64"]="linux/amd64"
    arch_docker_platform["arm64"]="linux/arm64"
    arch_docker_platform["armhf"]="linux/arm/v7"

    local -a deb_archs=("amd64" "arm64" "armhf")
    local out_abs
    out_abs="$(cd "${DEB_ALL_OUTPUT_DIR}" && pwd)"

    for deb_arch in "${deb_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${deb_arch}]}"
        local builder_tag="${DEB_ALL_BUILDER_IMAGE_PREFIX}:${deb_arch}"
        local arch_build_dir="${DEB_ALL_BUILD_DIR}/${deb_arch}"
        local bin_dir="${arch_build_dir}/pyinstaller"
        local work_dir="${arch_build_dir}/work"
        local stage_dir="${arch_build_dir}/${DEB_PKG_NAME}_${deb_version}_${deb_arch}"
        mkdir -p "${bin_dir}" "${work_dir}"

        echo
        log_step "─── ${deb_arch} build ───────────────────────────────────────"

        log_step "Building Docker image ${builder_tag}..."
        DOCKER_BUILDKIT=1 docker build \
            --platform "${docker_platform}" \
            --build-arg TARGET_PLATFORM="${docker_platform}" \
            -t "${builder_tag}" \
            -f "${DEB_ALL_DIR}/Dockerfile.builder" \
            "${DEB_ALL_DIR}"

        log_step "Compiling ${deb_arch} binary inside container..."
        docker run --rm \
            --platform "${docker_platform}" \
            -u "$(id -u):$(id -g)" \
            -e HOME=/tmp -e XDG_CACHE_HOME=/tmp \
            -v "${repo_root}:/repo" \
            -w /repo/hsync-server \
            -e LC_ALL=C -e PYTHONWARNINGS=ignore \
            -e PIP_DEFAULT_TIMEOUT=300 -e PIP_RETRIES=10 \
            -e POETRY_REQUESTS_TIMEOUT=300 \
            -e HSYNC_VERSION="${version}" \
            "${builder_tag}" \
            bash -c "
                set -euo pipefail
                poetry config virtualenvs.in-project true
                poetry config installer.max-workers 4
                install_ok=0
                for attempt in 1 2 3; do
                    if poetry install --only main --with build; then install_ok=1; break; fi
                    echo \"poetry install attempt \${attempt}/3 failed, retrying in 10s...\" >&2
                    sleep 10
                done
                [ \"\${install_ok}\" -eq 1 ] || { echo 'poetry install failed after 3 attempts' >&2; exit 1; }
                HSYNC_PREFER_PATH_PYINSTALLER=1 poetry run pyinstaller \
                    ${pyinstaller_flags} \
                    --distpath ${bin_dir} \
                    --workpath ${work_dir} \
                    --specpath ${work_dir}
            "

        local built_dir="${bin_dir}/${APP_NAME}"
        if [[ ! -d "${built_dir}" ]]; then
            log_error "${deb_arch} build directory not produced at ${built_dir}"
            exit 1
        fi
        log_ok "${deb_arch} binary ready ($(du -sh "${built_dir}" | cut -f1))"

        log_step "Staging .deb tree for ${deb_arch}..."
        rm -rf "${stage_dir}"
        mkdir -p "${stage_dir}"
        stage_abs="$(cd "${stage_dir}" && pwd)"

        cp -a "${DEB_SRC_CONTROL}" "${stage_dir}/"
        chmod 0755 "${stage_dir}/DEBIAN"/*inst* "${stage_dir}/DEBIAN"/*rm* 2>/dev/null || true

        local item
        for item in "${DEB_SRC_DATA}"/*; do
            local bn
            bn="$(basename "${item}")"
            if [[ "${bn}" == "DEBIAN" || "${bn}" == "Dockerfile.builder" ]]; then
                continue
            fi
            cp -a "${item}" "${stage_dir}/"
        done

        mkdir -p "${stage_dir}/usr/lib"
        cp -a "${built_dir}" "${stage_dir}/usr/lib/${APP_NAME}"
        chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"

        mkdir -p "${stage_dir}/usr/bin"
        ln -sf "../lib/${APP_NAME}/${APP_NAME}" "${stage_dir}/usr/bin/${APP_NAME}"

        _finalize_deb_stage "${stage_dir}"

        sed -i.bak \
            -e "s/__VERSION__/${deb_version}/g" \
            -e "s/__ARCH__/${deb_arch}/g" \
            "${stage_dir}/DEBIAN/control"
        rm -f "${stage_dir}/DEBIAN/control.bak"

        log_step "Building .deb for ${deb_arch}..."
        docker run --rm \
            --platform "${docker_platform}" \
            -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
            -v "${stage_abs}:/pkg" \
            -v "${out_abs}:/out" \
            -w /pkg \
            "${builder_tag}" \
            bash -c "
                set -e
                bin=/pkg/usr/lib/${APP_NAME}/${APP_NAME}
                tmp=\$(mktemp -d)
                mkdir -p \"\${tmp}/debian\"
                printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' '${DEB_PKG_NAME}' '${DEB_PKG_NAME}' > \"\${tmp}/debian/control\"
                deps=\$(cd \"\${tmp}\" && dpkg-shlibdeps -O --ignore-missing-info \"\${bin}\" 2>/dev/null | sed -n 's/^shlibs:Depends=//p')
                rm -rf \"\${tmp}\"
                if [ -n \"\${deps}\" ]; then
                    sed -i \"s/^Depends: \\(.*\\)\$/Depends: \\1, \${deps}/\" /pkg/DEBIAN/control
                    echo \"Added shared-library dependencies: \${deps}\"
                else
                    echo 'WARNING: dpkg-shlibdeps resolved no dependencies' >&2
                fi
                dpkg-deb --build /pkg /out
                chmod 0644 /out/${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb
                chown -R \"\${HOST_UID}:\${HOST_GID}\" /pkg \"/out/${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb\"
            "

        local deb_name="${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb"
        local deb_path="${out_abs}/${deb_name}"
        if [[ ! -f "${deb_path}" ]]; then
            log_error ".deb not produced for ${deb_arch} at ${deb_path}"
            exit 1
        fi
        local pkg_size
        pkg_size="$(du -h "${deb_path}" | cut -f1)"
        log_ok "  → ${deb_name} (${pkg_size})"
    done

    echo
    log_ok "All 3 Debian packages built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}   ${deb_version}"
    echo -e "  ${BOLD}Output:${RESET}    ${out_abs}/"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation packages:${RESET}"
    for deb_arch in "${deb_archs[@]}"; do
        local f="${out_abs}/${DEB_PKG_NAME}_${version}_${deb_arch}.deb"
        [[ -f "${f}" ]] && echo -e "${GREEN}${BOLD}      $(basename "${f}")${RESET}"
    done
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo
    echo -e "${DIM}Install with:  sudo dpkg -i ${out_abs}/${DEB_PKG_NAME}_${version}_<arch>.deb${RESET}"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    local do_clean=false
    local target="native"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--clean)
                do_clean=true
                shift
                ;;
            -t|--target)
                [[ $# -ge 2 ]] || { log_error "--target requires a value"; exit 1; }
                target="$2"
                shift 2
                ;;
            -v|--version)
                [[ $# -ge 2 ]] || { log_error "--version requires a value"; exit 1; }
                VERSION_OVERRIDE="$2"
                shift 2
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Use --help for usage information."
                exit 1
                ;;
        esac
    done

    # Change to script directory
    cd "${SCRIPT_DIR}"

    # Print header
    echo -e "${BOLD}HSYNC Server - Build${RESET}"
    echo -e "${DIM}────────────────────${RESET}"
    echo

    case "${target}" in
        native)
            check_prerequisites
            if [[ "${do_clean}" == true ]]; then
                clean_artifacts
                echo
            fi
            build_native
            ;;
        rpi)
            # Only pyproject.toml is required (poetry runs inside the container).
            if [[ ! -f "${PYPROJECT}" ]]; then
                log_error "${PYPROJECT} not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning rpi artifacts..."
                rm -rf "${RPI_OUTPUT_DIR}"
                docker image rm -f \
                    "${RPI_BUILDER_IMAGE_PREFIX}:armhf" \
                    "${RPI_BUILDER_IMAGE_PREFIX}:arm64" >/dev/null 2>&1 || true
                echo
            fi
            build_rpi
            ;;
        deb)
            check_prerequisites
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning deb artifacts..."
                rm -rf "${DEB_OUTPUT_DIR}" "${DEB_BUILD_DIR}"
                echo
            fi
            build_deb
            ;;
        deb-all)
            # Only pyproject.toml is required (poetry runs inside the container).
            if [[ ! -f "${PYPROJECT}" ]]; then
                log_error "${PYPROJECT} not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning deb-all artifacts..."
                rm -rf "${DEB_ALL_OUTPUT_DIR}" "${DEB_ALL_BUILD_DIR}"
                docker image rm -f \
                    "${DEB_ALL_BUILDER_IMAGE_PREFIX}:amd64" \
                    "${DEB_ALL_BUILDER_IMAGE_PREFIX}:arm64" \
                    "${DEB_ALL_BUILDER_IMAGE_PREFIX}:armhf" >/dev/null 2>&1 || true
                echo
            fi
            build_deb_all
            ;;
        *)
            log_error "Unknown target: ${target}"
            echo "Supported targets: native, deb, deb-all, rpi"
            exit 1
            ;;
    esac
}

main "$@"
