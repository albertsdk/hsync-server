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
# Single host data directory for everything that must survive the container:
# user buckets, the causal-state database and the TLS identity all live under
# this one directory as plain files, so backup, inspection and host-to-host
# migration are ordinary file operations. The container sees them at the
# canonical /project/{buckets,state,certs} mount points. Docker requires
# absolute bind-mount sources, so a relative HSYNC_DATA_DIR is resolved
# against the script directory (it may not exist yet at parse time).
_DATA_DIR_RAW="${HSYNC_DATA_DIR:-${SCRIPT_DIR}/hsync-data}"
if [[ "${_DATA_DIR_RAW}" = /* ]]; then
    readonly DATA_DIR="${_DATA_DIR_RAW%/}"
else
    readonly DATA_DIR="${SCRIPT_DIR}/${_DATA_DIR_RAW%/}"
fi
unset _DATA_DIR_RAW
readonly BUCKETS_HOST_DIR="${DATA_DIR}/buckets"
readonly STATE_HOST_DIR="${DATA_DIR}/state"
readonly CERTS_HOST_DIR="${DATA_DIR}/certs"
# Remembers the env-file used by the last successful `start`, so `restart` can
# transparently reuse it without re-specifying --env-file. Local state only.
readonly ENV_STATE_FILE="${SCRIPT_DIR}/.hsync-last-env-file"
# Seconds to wait for the freshly started container to answer /health before
# declaring the start successful. First boot generates the TLS certificate,
# which can take a few seconds. Set HSYNC_STARTUP_HEALTH_TIMEOUT=0 to skip the
# check (the URL is then printed without waiting).
STARTUP_HEALTH_TIMEOUT="${HSYNC_STARTUP_HEALTH_TIMEOUT:-30}"

# Default environment variables
export HSYNC_PORT="${HSYNC_PORT:-9753}"
export HSYNC_LOG_LEVEL="${HSYNC_LOG_LEVEL:-info}"
export HSYNC_WORKERS="${HSYNC_WORKERS:-1}"
export HSYNC_ADMIN_KEY="${HSYNC_ADMIN_KEY:-}"
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
    clean       Remove container and image (add --all to also drop buckets/state)

${BOLD}Options:${RESET}
    -e, --env-file  Environment file to pass to Docker (e.g., dev.env, prod.env)
    -f, --follow    Follow log output (for logs command)
    -h, --help      Show this help message

${BOLD}Environment Variables:${RESET}
    HSYNC_PORT        Server port (default: 9753)
    HSYNC_LOG_LEVEL   Log level: debug, info, warning, error (default: info)
    HSYNC_WORKERS     Number of workers (default: 1)
    HSYNC_ADMIN_KEY   Bootstrap admin secret (64 lowercase hex characters)
    HSYNC_TZ          Timezone in IANA format (default: UTC)
    HSYNC_VERSION     Image tag version (default: 1.0.0)
    HSYNC_DATA_DIR    Host data directory (default: ./hsync-data)

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

Start prints the server URL (e.g. https://localhost:9753) as soon as the
container answers /health; no need to read the logs. The URL uses HTTPS
when HSYNC_SSL_ENABLED=true in the env-file (the default in dev.env and
prod.env) with a self-signed certificate: use curl -k or accept the browser
warning on first visit.

All persistent data lives on the host under ${DATA_DIR}:
    buckets/   user files (one subdirectory per bucket)
    state/     the causal-state database (syncstate.db)
    certs/     the self-signed TLS certificate and key
Back up the whole directory; restore it to move the server to another host.
EOF
}

# -----------------------------------------------------------------------------
# Data directory helpers
# -----------------------------------------------------------------------------
ensure_data_dirs() {
    local dir
    for dir in "${BUCKETS_HOST_DIR}" "${STATE_HOST_DIR}" "${CERTS_HOST_DIR}"; do
        if [[ ! -d "${dir}" ]]; then
            mkdir -p "${dir}"
            log_info "Created data directory: ${dir}"
        fi
    done
}

ssl_enabled() {
    local value="${HSYNC_SSL_ENABLED:-}"
    [[ -z "${value}" ]] && value="$(get_env_file_value HSYNC_SSL_ENABLED || true)"
    [[ "${value,,}" == "true" || "${value}" == "1" ]]
}

docs_enabled() {
    local value="${HSYNC_ENABLE_DOCS:-}"
    [[ -z "${value}" ]] && value="$(get_env_file_value HSYNC_ENABLE_DOCS || true)"
    [[ "${value,,}" == "true" || "${value}" == "1" ]]
}

health_scheme() {
    if ssl_enabled; then
        printf 'https'
    else
        printf 'http'
    fi
}

wait_for_health() {
    local scheme url code deadline
    scheme="$(health_scheme)"
    url="${scheme}://localhost:${HSYNC_PORT}/health"
    deadline=$(( $(date +%s) + STARTUP_HEALTH_TIMEOUT ))
    while (( $(date +%s) < deadline )); do
        if ssl_enabled; then
            code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 2 "${url}" 2>/dev/null || true)"
        else
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${url}" 2>/dev/null || true)"
        fi
        [[ "${code}" == "200" ]] && return 0
        sleep 1
    done
    return 1
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

    if [[ -z "${HSYNC_ADMIN_KEY}" ]] && ! env_file_has_nonempty_key HSYNC_ADMIN_KEY; then
        log_error "HSYNC_ADMIN_KEY is required"
        log_info "Set it via environment variable or use --env-file option"
        exit 1
    fi

    if docker ps -q -f name="${CONTAINER_NAME}" | grep -q .; then
        log_warn "Container ${CONTAINER_NAME} is already running"
        return
    fi

    log_step "Starting container ${CONTAINER_NAME}..."

    ensure_data_dirs

    # Build docker run command with env file support
    local docker_args=(
        run -d
        --name "${CONTAINER_NAME}"
        --restart unless-stopped
        -p "${HSYNC_PORT}:${HSYNC_PORT}"
        # Run under the invoking user's ids so the bind-mounted data
        # directories stay writable (created above as the same user).
        --user "${HSYNC_UID:-$(id -u)}:${HSYNC_GID:-$(id -g)}"
        -v "${BUCKETS_HOST_DIR}:/project/buckets"
        -v "${STATE_HOST_DIR}:/project/state"
        -v "${CERTS_HOST_DIR}:/project/certs"
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
    if [[ -n "${HSYNC_ADMIN_KEY}" ]]; then
        docker_args+=(-e HSYNC_ADMIN_KEY)
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

    local scheme
    scheme="$(health_scheme)"

    log_info "Port: ${HSYNC_PORT}, Workers: ${HSYNC_WORKERS}, Log Level: ${HSYNC_LOG_LEVEL}, TZ: ${HSYNC_TZ}"
    log_info "Data: ${DATA_DIR}"

    if [[ "${STARTUP_HEALTH_TIMEOUT}" =~ ^[0-9]+$ ]] && (( STARTUP_HEALTH_TIMEOUT > 0 )); then
        log_step "Waiting for the server to answer /health (up to ${STARTUP_HEALTH_TIMEOUT}s)..."
        if ! wait_for_health; then
            log_error "Server did not answer ${scheme}://localhost:${HSYNC_PORT}/health within ${STARTUP_HEALTH_TIMEOUT}s"
            log_error "Last container logs:"
            docker logs --tail 30 "${CONTAINER_NAME}" >&2 || true
            exit 1
        fi
        log_ok "HSYNC Server is live → ${scheme}://localhost:${HSYNC_PORT}"
    else
        log_info "Health check skipped (HSYNC_STARTUP_HEALTH_TIMEOUT=0): server may still be starting"
        log_info "Expected server URL → ${scheme}://localhost:${HSYNC_PORT}"
    fi

    if docs_enabled; then
        log_ok "API documentation: ${scheme}://localhost:${HSYNC_PORT}/docs"
    else
        log_info "API docs are disabled (set HSYNC_ENABLE_DOCS=true in the env-file to expose /docs)"
    fi
    if ssl_enabled; then
        log_warn "Self-signed certificate: use curl -k or accept the browser warning"
    fi
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
    local clean_all=0
    [[ "${1:-}" == "--all" ]] && clean_all=1
    log_step "Cleaning up..."
    docker stop "${CONTAINER_NAME}" 2>/dev/null || true
    docker rm "${CONTAINER_NAME}" 2>/dev/null || true
    docker rmi "${IMAGE_NAME}:${IMAGE_TAG}" 2>/dev/null || true
    # Drop the persisted TLS material: a fresh image deserves a fresh
    # self-signed identity. Connecting clients will re-pin on next handshake.
    rm -rf "${CERTS_HOST_DIR}"
    if (( clean_all )); then
        log_warn "Removing ${DATA_DIR} (ALL user data will be lost)"
        rm -rf "${DATA_DIR}"
    else
        log_info "Keeping ${BUCKETS_HOST_DIR} and ${STATE_HOST_DIR} (use 'clean --all' to drop them)"
    fi
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
        clean)   cmd_clean "${2:-}" ;;
        -h|--help|help|"") show_help ;;
        *) log_error "Unknown command: $1"; show_help; exit 1 ;;
    esac
}

main "$@"
