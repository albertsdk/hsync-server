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
#                               rpi                  Standalone executables for
#                                                    Raspberry Pi OS 32-bit
#                                                    (armhf) and 64-bit (arm64)
#                                                    via Docker
#   -v, --version VERSION   Override the build version reported in the build
#                           log (default: the pyproject.toml version). Also
#                           settable via the HSYNC_VERSION environment variable.
#   -h, --help              Show this help message
#
# Requirements:
#   - native target: Python virtual environment (.venv) with the build group
#     installed; run 'poetry install --with build'
#   - rpi target: Docker with qemu-user-static (binfmt) configured (used for
#     cross-arch emulation on amd64 hosts). Dependencies are installed inside
#     the container, so the host virtualenv is not used.
#
# Output:
#   native:
#     - dist/<os>-<arch>/hsync-server[.exe]   Standalone platform-aware
#       executable, e.g. dist/linux-amd64/hsync-server
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
                            Supported: native, rpi
    -v, --version VERSION   Override the version shown in the build log
                            (default: pyproject.toml). Also settable via
                            HSYNC_VERSION. The frozen binary's own reported
                            version is fixed at build time (hsync_server
                            .constants), independent of this value.
    -h, --help              Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh                        # Native host build
    ./compile.sh --clean                # Clean and rebuild
    ./compile.sh --target rpi           # Raspberry Pi armhf + arm64

${BOLD}Output:${RESET}
    native:
        ${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}   Standalone executable (platform: ${PLATFORM})
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
        *)
            log_error "Unknown target: ${target}"
            echo "Supported targets: native, rpi"
            exit 1
            ;;
    esac
}

main "$@"
