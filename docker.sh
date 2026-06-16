#!/usr/bin/env bash
#
# HSYNC Server - Docker Management Script
# Unified script for Docker operations: build, start, stop, restart, logs, etc.
#
# Usage: ./docker.sh <command> [options]
#
# Author: Alberto Basso
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repository root: build context must include the sibling ../hsync-common package
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly IMAGE_NAME="hsync"
readonly IMAGE_TAG="${HSYNC_VERSION:-1.0.0}"
readonly CONTAINER_NAME="hsync-server"

# Default environment variables
export HSYNC_PORT="${HSYNC_PORT:-8765}"
export HSYNC_LOG_LEVEL="${HSYNC_LOG_LEVEL:-info}"
export HSYNC_WORKERS="${HSYNC_WORKERS:-1}"
export HSYNC_API_KEY="${HSYNC_API_KEY:-}"
export HSYNC_TZ="${HSYNC_TZ:-UTC}"

# Environment file (can be set via --env-file option)
ENV_FILE=""

# -----------------------------------------------------------------------------
# Colors (disabled if not a terminal)
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
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
log_info()  { printf '%s\n' "${BLUE}ℹ${RESET} $*"; }
log_ok()    { printf '%s\n' "${GREEN}✓${RESET} $*"; }
log_warn()  { printf '%s\n' "${YELLOW}⚠${RESET} $*"; }
log_error() { printf '%s\n' "${RED}✕${RESET} $*" >&2; }
log_step()  { printf '%s\n' "${CYAN}→${RESET} ${BOLD}$*${RESET}"; }

show_help() {
    cat << EOF
${BOLD}HSYNC Server - Docker Management${RESET}

${BOLD}Usage:${RESET} ./docker.sh <command> [options]

${BOLD}Commands:${RESET}
    build       Build the Docker image
    start       Start the container
    stop        Stop the container
    restart     Restart the container
    status      Show container status
    logs        Show container logs (use -f to follow)
    shell       Open a shell in the running container
    clean       Remove container and image

${BOLD}Options:${RESET}
    -e, --env-file  Environment file to load (e.g., dev.env, prod.env)
    -f, --follow    Follow log output (for logs command)
    -h, --help      Show this help message

${BOLD}Environment Variables:${RESET}
    HSYNC_PORT        Server port (default: 8765)
    HSYNC_LOG_LEVEL   Log level: debug, info, warning, error (default: info)
    HSYNC_WORKERS     Number of workers (default: 1)
    HSYNC_API_KEY     API key for authentication (required)
    HSYNC_TZ          Timezone in IANA format (default: UTC)
    HSYNC_VERSION     Image tag version (default: 1.0.0)

${BOLD}Examples:${RESET}
    ./docker.sh build
    ./docker.sh start --env-file dev.env
    ./docker.sh start --env-file prod.env
    ./docker.sh logs -f
    ./docker.sh shell
EOF
}

cmd_build() {
    log_step "Building Docker image ${IMAGE_NAME}:${IMAGE_TAG}..."
    docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" "${REPO_ROOT}"
    log_ok "Image built: ${IMAGE_NAME}:${IMAGE_TAG}"
}

cmd_start() {
    # Load environment file if specified
    if [[ -n "${ENV_FILE}" ]]; then
        if [[ -f "${ENV_FILE}" ]]; then
            log_info "Loading environment from ${ENV_FILE}"
            # shellcheck disable=SC1090
            set -a
            source "${ENV_FILE}"
            set +a
        else
            log_error "Environment file not found: ${ENV_FILE}"
            exit 1
        fi
    fi

    if [[ -z "${HSYNC_API_KEY}" ]]; then
        log_error "HSYNC_API_KEY is required"
        log_info "Set it via environment variable or use --env-file option"
        exit 1
    fi

    if docker ps -q -f name="${CONTAINER_NAME}" | grep -q .; then
        log_warn "Container ${CONTAINER_NAME} is already running"
        return
    fi

    log_step "Starting container ${CONTAINER_NAME}..."

    # Build docker run command with env file support
    local docker_args=(
        run -d
        --name "${CONTAINER_NAME}"
        --restart unless-stopped
        -p "${HSYNC_PORT}:${HSYNC_PORT}"
        -e "HSYNC_PORT=${HSYNC_PORT}"
        -e "HSYNC_API_KEY=${HSYNC_API_KEY}"
        -e "HSYNC_LOG_LEVEL=${HSYNC_LOG_LEVEL}"
        -e "HSYNC_WORKERS=${HSYNC_WORKERS}"
        -e "HSYNC_TZ=${HSYNC_TZ}"
        -v "${SCRIPT_DIR}/buckets:/project/buckets"
    )

    docker "${docker_args[@]}" "${IMAGE_NAME}:${IMAGE_TAG}"
    log_ok "Container started: ${CONTAINER_NAME}"
    log_info "Port: ${HSYNC_PORT}, Workers: ${HSYNC_WORKERS}, Log Level: ${HSYNC_LOG_LEVEL}, TZ: ${HSYNC_TZ}"
}

cmd_stop() {
    log_step "Stopping container ${CONTAINER_NAME}..."
    docker stop "${CONTAINER_NAME}" 2>/dev/null || true
    docker rm "${CONTAINER_NAME}" 2>/dev/null || true
    log_ok "Container stopped"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    printf '%s\n' "${BOLD}Container Status:${RESET}"
    docker ps -a --filter name="${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
}

cmd_logs() {
    local follow=""
    [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]] && follow="-f"
    docker logs ${follow} "${CONTAINER_NAME}"
}

cmd_shell() {
    log_step "Opening shell in ${CONTAINER_NAME}..."
    docker exec -it "${CONTAINER_NAME}" /bin/sh
}

cmd_clean() {
    log_step "Cleaning up..."
    docker stop "${CONTAINER_NAME}" 2>/dev/null || true
    docker rm "${CONTAINER_NAME}" 2>/dev/null || true
    docker rmi "${IMAGE_NAME}:${IMAGE_TAG}" 2>/dev/null || true
    log_ok "Cleanup complete"
}



# -----------------------------------------------------------------------------
# Parse Options
# -----------------------------------------------------------------------------
parse_options() {
    local args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -e|--env-file)
                if [[ -n "${2:-}" ]]; then
                    ENV_FILE="$2"
                    shift 2
                else
                    log_error "Option $1 requires an argument"
                    exit 1
                fi
                ;;
            *)
                args+=("$1")
                shift
                ;;
        esac
    done
    # Return remaining args for processing
    ARGS=("${args[@]}")
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    cd "${SCRIPT_DIR}"

    # Parse global options
    parse_options "$@"
    set -- "${ARGS[@]}"

    case "${1:-}" in
        build)   cmd_build ;;
        start)   cmd_start ;;
        stop)    cmd_stop ;;
        restart) cmd_restart ;;
        status)  cmd_status ;;
        logs)    cmd_logs "${2:-}" ;;
        shell)   cmd_shell ;;
        clean)   cmd_clean ;;
        -h|--help|help|"") show_help ;;
        *) log_error "Unknown command: $1"; show_help; exit 1 ;;
    esac
}

main "$@"
