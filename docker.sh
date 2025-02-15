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
# Named volume for SSL material: persisting the auto-generated self-signed
# certificate across container recreation keeps the server identity (and the
# client's TOFU fingerprint pin) stable. A named volume inherits the image's
# hsync:hsync ownership, so the unprivileged user can write the encrypted key.
readonly CERTS_VOLUME="${CONTAINER_NAME}-certs"
# Remembers the env-file used by the last successful `start`, so `restart` can
# transparently reuse it without re-specifying --env-file. Local state only.
readonly ENV_STATE_FILE="${SCRIPT_DIR}/.hsync-last-env-file"

# Default environment variables
export HSYNC_PORT="${HSYNC_PORT:-8765}"
export HSYNC_LOG_LEVEL="${HSYNC_LOG_LEVEL:-info}"
export HSYNC_WORKERS="${HSYNC_WORKERS:-1}"
export HSYNC_API_KEY="${HSYNC_API_KEY:-}"
export HSYNC_TZ="${HSYNC_TZ:-UTC}"

# Environment file (can be set via --env-file option)
ENV_FILE=""

get_env_file_value() {
    local key="$1"
    [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]] || return 1
    awk -F= -v key="${key}" '
        /^[[:space:]]*(#|$)/ { next }
        {
            name=$1
            sub(/^[[:space:]]*export[[:space:]]+/, "", name)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            if (name == key) {
                value=$0
                sub(/^[^=]*=/, "", value)
                sub(/[[:space:]]+#.*$/, "", value)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                gsub(/^"|"$/, "", value)
                gsub(/^'\''|'\''$/, "", value)
                print value
            }
        }
    ' "${ENV_FILE}" | tail -n 1
}

env_file_has_nonempty_key() {
    local key="$1"
    [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]] || return 1
    grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*[^[:space:]#]+" "${ENV_FILE}"
}

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
    -e, --env-file  Environment file to pass to Docker (e.g., dev.env, prod.env)
    -f, --follow    Follow log output (for logs command)
    -h, --help      Show this help message

${BOLD}Environment Variables:${RESET}
    HSYNC_PORT        Server port (default: 8765)
    HSYNC_LOG_LEVEL   Log level: debug, info, warning, error (default: info)
    HSYNC_WORKERS     Number of workers (default: 1)
    HSYNC_API_KEY     API key for authentication (required)
    HSYNC_TZ          Timezone in IANA format (default: UTC)
    HSYNC_VERSION     Image tag version (default: 1.0.0)

    SSL/TLS (forwarded by name from the current environment):
    HSYNC_SSL_ENABLED           Enable HTTPS/WSS (true/false)
    HSYNC_SSL_KEYFILE_PASSWORD  Password for the encrypted private key (required when SSL on)
    HSYNC_SSL_CERTFILE          Path to SSL certificate (PEM)
    HSYNC_SSL_KEYFILE           Path to SSL private key (PEM)
    HSYNC_SSL_CERT_DIR          Directory for auto-generated certificates

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
    # Validate and use environment file if specified. Do not source it: env files
    # may contain secrets and shell code; Docker can consume them directly.
    if [[ -n "${ENV_FILE}" ]]; then
        if [[ -f "${ENV_FILE}" ]]; then
            log_info "Using environment file ${ENV_FILE}"
            env_port="$(get_env_file_value HSYNC_PORT || true)"
            env_log_level="$(get_env_file_value HSYNC_LOG_LEVEL || true)"
            env_workers="$(get_env_file_value HSYNC_WORKERS || true)"
            env_tz="$(get_env_file_value HSYNC_TZ || true)"
            [[ -n "${env_port}" ]] && HSYNC_PORT="${env_port}"
            [[ -n "${env_log_level}" ]] && HSYNC_LOG_LEVEL="${env_log_level}"
            [[ -n "${env_workers}" ]] && HSYNC_WORKERS="${env_workers}"
            [[ -n "${env_tz}" ]] && HSYNC_TZ="${env_tz}"
        else
            log_error "Environment file not found: ${ENV_FILE}"
            exit 1
        fi
    fi

    if [[ -z "${HSYNC_API_KEY}" ]] && ! env_file_has_nonempty_key HSYNC_API_KEY; then
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
        -v "${SCRIPT_DIR}/buckets:/project/buckets"
        -v "${CERTS_VOLUME}:/project/certs"
    )

    if [[ -n "${ENV_FILE}" ]]; then
        docker_args+=(--env-file "${ENV_FILE}")
    else
        docker_args+=(
            -e "HSYNC_PORT=${HSYNC_PORT}"
            -e "HSYNC_LOG_LEVEL=${HSYNC_LOG_LEVEL}"
            -e "HSYNC_WORKERS=${HSYNC_WORKERS}"
            -e "HSYNC_TZ=${HSYNC_TZ}"
        )
    fi

    # Pass the API key by name only when it comes from the parent environment,
    # avoiding exposure as a command-line argument.
    if [[ -n "${HSYNC_API_KEY}" ]]; then
        docker_args+=(-e HSYNC_API_KEY)
    fi

    # Forward SSL/TLS settings from the parent environment by name. Passing them
    # by name (not as VAR=value) keeps the secret HSYNC_SSL_KEYFILE_PASSWORD off
    # the command line, and ensures `HSYNC_SSL_ENABLED=true ./docker.sh start`
    # actually reaches the server (the entrypoint reads these to enable HTTPS).
    for ssl_var in HSYNC_SSL_ENABLED HSYNC_SSL_KEYFILE_PASSWORD \
        HSYNC_SSL_CERTFILE HSYNC_SSL_KEYFILE HSYNC_SSL_CERT_DIR; do
        if [[ -n "${!ssl_var:-}" ]]; then
            docker_args+=(-e "${ssl_var}")
        fi
    done

    docker "${docker_args[@]}" "${IMAGE_NAME}:${IMAGE_TAG}"

    # Remember the env-file (if any) so `restart` can reuse it transparently;
    # clear the record when started without one to avoid reusing a stale file.
    if [[ -n "${ENV_FILE}" ]]; then
        printf '%s\n' "${ENV_FILE}" > "${ENV_STATE_FILE}"
    else
        rm -f "${ENV_STATE_FILE}"
    fi

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
    # Reuse the env-file from the last start unless one was passed explicitly,
    # so `restart` keeps the same configuration (API key, SSL, ...) as `start`.
    if [[ -z "${ENV_FILE}" && -f "${ENV_STATE_FILE}" ]]; then
        local remembered
        remembered="$(cat "${ENV_STATE_FILE}" 2>/dev/null || true)"
        if [[ -n "${remembered}" && -f "${remembered}" ]]; then
            ENV_FILE="${remembered}"
            log_info "Reusing env-file from last start: ${ENV_FILE}"
        elif [[ -n "${remembered}" ]]; then
            log_warn "Remembered env-file no longer exists: ${remembered}"
        fi
    fi
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
    # Drop the persisted SSL material too: a fresh image deserves a fresh
    # self-signed identity. Connecting clients will re-pin on next handshake.
    docker volume rm "${CERTS_VOLUME}" 2>/dev/null || true
    # Forget the remembered env-file: a clean slate should not auto-reuse it.
    rm -f "${ENV_STATE_FILE}"
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
