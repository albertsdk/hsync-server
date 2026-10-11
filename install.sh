#!/usr/bin/env bash

set -euo pipefail

readonly DEFAULT_VERSION="v1.0.0"
readonly DEFAULT_INSTALL_DIR="${HOME}/.local/share/hsync-server"
readonly REPO="albertsdk/hsync-server"

INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
VERSION="${DEFAULT_VERSION}"
BIND_ADDRESS="127.0.0.1"
PUBLIC_PORT="9753"
BIND_ADDRESS_SET=false
PUBLIC_PORT_SET=false
START_SERVER=true
DRY_RUN=false

usage() {
    cat <<EOF
Install HSYNC Server with Docker Compose.

Usage: ./install.sh [options]

Options:
  --version VERSION       Image version to install, or 'latest' to resolve the
                          latest stable release automatically (default: ${DEFAULT_VERSION})
  --install-dir PATH      Configuration directory
  --bind-address ADDRESS  Concrete host IPv4 address (default: 127.0.0.1)
  --port PORT             Host port (default: 9753)
  --no-start              Generate and validate files without starting the server
  --dry-run               Show the planned configuration without changing files
  -h, --help              Show this help

The installer never installs Docker, invokes sudo, opens firewall ports, or
overwrites an existing partial installation. Re-running it updates the selected
image version while preserving existing secrets and data.
EOF
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

validate_version() {
    [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] ||
        fail "invalid version '$1'; expected vX.Y.Z or 'latest'"
}

resolve_latest_version() {
    local url effective tag
    url="https://github.com/${REPO}/releases/latest"
    effective="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "${url}")" ||
        fail "could not resolve the latest release from ${url}"
    tag="${effective##*/}"
    [[ "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] ||
        fail "unexpected latest release tag '${tag}' from ${url}"
    printf '%s' "${tag}"
}

validate_ipv4() {
    local address="$1"
    local octet
    local -a octets

    IFS=. read -r -a octets <<< "${address}"
    [[ ${#octets[@]} -eq 4 ]] || fail "invalid IPv4 bind address: ${address}"
    for octet in "${octets[@]}"; do
        [[ "${octet}" =~ ^[0-9]{1,3}$ ]] && ((10#${octet} <= 255)) ||
            fail "invalid IPv4 bind address: ${address}"
    done
    [[ "${address}" != "0.0.0.0" ]] ||
        fail "0.0.0.0 cannot identify the TLS certificate; use a concrete host address"
}

validate_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)) ||
        fail "invalid port '$1'; expected 1-65535"
}

# True when the docker command comes from the snap package. Snap confinement is
# relevant because the "home" interface denies hidden paths under $HOME.
docker_is_snap() {
    local docker_path
    docker_path="$(command -v docker 2>/dev/null)" || return 1
    case "${docker_path}" in
        /snap/* | */snap/*) return 0 ;;
    esac
    # A wrapper/symlink pointing into the snap tree (readlink without -f stays
    # portable across GNU/BSD userlands).
    case "$(readlink "${docker_path}" 2>/dev/null || true)" in
        /snap/* | */snap/*) return 0 ;;
    esac
    return 1
}

# True when an absolute path contains a hidden component (a name starting with
# '.'), which the snap "home" interface refuses to expose.
path_has_hidden_component() {
    local component
    local IFS='/'
    # shellcheck disable=SC2086
    for component in ${1#/}; do
        case "${component}" in
            "." | "..") continue ;;
            .*) return 0 ;;
        esac
    done
    return 1
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                VERSION="$2"
                shift 2
                ;;
            --install-dir)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                INSTALL_DIR="$2"
                shift 2
                ;;
            --bind-address)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                BIND_ADDRESS="$2"
                BIND_ADDRESS_SET=true
                shift 2
                ;;
            --port)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                PUBLIC_PORT="$2"
                PUBLIC_PORT_SET=true
                shift 2
                ;;
            --no-start)
                START_SERVER=false
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                fail "unknown option: $1"
                ;;
        esac
    done
}

write_compose_file() {
    cat > "${INSTALL_DIR}/compose.yaml" <<'EOF'
name: hsync-server

services:
  server:
    image: ${HSYNC_IMAGE:?Set HSYNC_IMAGE in .env}
    hostname: ${HSYNC_CERT_HOSTNAME:-127.0.0.1}
    restart: unless-stopped
    user: "${HSYNC_UID:-1000}:${HSYNC_GID:-1000}"
    ports:
      - "${HSYNC_BIND_ADDRESS:-127.0.0.1}:${HSYNC_PUBLIC_PORT:-9753}:9753"
    env_file:
      - .env
    volumes:
      - ${HSYNC_DATA_DIR:-./hsync-data}/buckets:/project/buckets
      - ${HSYNC_DATA_DIR:-./hsync-data}/state:/project/state
      - ${HSYNC_DATA_DIR:-./hsync-data}/certs:/project/certs
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - >-
          import ssl, urllib.request;
          urllib.request.urlopen("https://127.0.0.1:9753/health",
          context=ssl._create_unverified_context(), timeout=5)
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 10s
EOF
}

write_env_file() {
    local api_key="$1"

    cat > "${INSTALL_DIR}/.env" <<EOF
# Generated by the HSYNC Server installer. Keep this file private.
HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:${VERSION}
HSYNC_BIND_ADDRESS=${BIND_ADDRESS}
HSYNC_CERT_HOSTNAME=${BIND_ADDRESS}
HSYNC_PUBLIC_PORT=${PUBLIC_PORT}
HSYNC_ADMIN_KEY=${api_key}
HSYNC_SSL_ENABLED=true
# Host directory holding buckets/, state/ and certs/ (bind-mounted into the
# container). Back up this whole directory; restoring it migrates the server.
HSYNC_DATA_DIR=${INSTALL_DIR}/hsync-data
# Container runs under these host ids so the bind-mounted data directories
# stay writable (they were created by this installer as the same user).
HSYNC_UID=$(id -u)
HSYNC_GID=$(id -g)
HSYNC_BUCKETS=/project/buckets
HSYNC_STATE_DIR=/project/state
HSYNC_SSL_CERT_DIR=/project/certs
EOF
    chmod 600 "${INSTALL_DIR}/.env"
}

update_env_value() {
    local key="$1"
    local value="$2"
    local env_file="${INSTALL_DIR}/.env"
    local temporary_file="${env_file}.tmp"

    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" == "${key}="* ]]; then
            printf '%s=%s\n' "${key}" "${value}"
        else
            printf '%s\n' "${line}"
        fi
    done < "${env_file}" > "${temporary_file}"
    mv "${temporary_file}" "${env_file}"
    chmod 600 "${env_file}"
}

read_env_value() {
    local wanted_key="$1"
    local key
    local value

    while IFS='=' read -r key value; do
        if [[ "${key}" == "${wanted_key}" ]]; then
            printf '%s\n' "${value}"
            return 0
        fi
    done < "${INSTALL_DIR}/.env"
    return 1
}

install_files() {
    local env_file="${INSTALL_DIR}/.env"
    local compose_file="${INSTALL_DIR}/compose.yaml"

    if [[ -e "${env_file}" || -e "${compose_file}" ]]; then
        [[ -f "${env_file}" && -f "${compose_file}" ]] ||
            fail "partial installation in ${INSTALL_DIR}; refusing to overwrite it"
        update_env_value HSYNC_IMAGE "ghcr.io/albertsdk/hsync-server:${VERSION}"
        if [[ "${BIND_ADDRESS_SET}" == true ]]; then
            update_env_value HSYNC_BIND_ADDRESS "${BIND_ADDRESS}"
            update_env_value HSYNC_CERT_HOSTNAME "${BIND_ADDRESS}"
        fi
        if [[ "${PUBLIC_PORT_SET}" == true ]]; then
            update_env_value HSYNC_PUBLIC_PORT "${PUBLIC_PORT}"
        fi
        printf 'Reusing existing secrets and data in %s\n' "${INSTALL_DIR}"
        return
    fi

    mkdir -p "${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}/hsync-data/buckets" \
        "${INSTALL_DIR}/hsync-data/state" \
        "${INSTALL_DIR}/hsync-data/certs"
    write_compose_file
    write_env_file "$(openssl rand -hex 32)"
    chmod 600 "${compose_file}"
}

main() {
    local endpoint_host
    local existing_bind_address

    parse_args "$@"

    if [[ "${INSTALL_DIR}" != /* ]]; then
        INSTALL_DIR="$(pwd)/${INSTALL_DIR}"
    fi

    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        existing_bind_address="$(read_env_value HSYNC_BIND_ADDRESS)" ||
            fail "HSYNC_BIND_ADDRESS is missing from ${INSTALL_DIR}/.env"
        if [[ "${BIND_ADDRESS_SET}" == true && "${BIND_ADDRESS}" != "${existing_bind_address}" ]]; then
            fail "cannot change the bind address of an existing TLS identity; create a new installation or rotate its certificate explicitly"
        fi
        BIND_ADDRESS="${existing_bind_address}"
        if [[ "${PUBLIC_PORT_SET}" == false ]]; then
            PUBLIC_PORT="$(read_env_value HSYNC_PUBLIC_PORT)" ||
                fail "HSYNC_PUBLIC_PORT is missing from ${INSTALL_DIR}/.env"
        fi
    fi

    if [[ "${VERSION}" == "latest" ]]; then
        require_command curl
        VERSION="$(resolve_latest_version)"
    fi
    validate_version "${VERSION}"
    validate_ipv4 "${BIND_ADDRESS}"
    validate_port "${PUBLIC_PORT}"

    endpoint_host="${BIND_ADDRESS}"

    printf 'Install directory: %s\n' "${INSTALL_DIR}"
    printf 'Image: ghcr.io/albertsdk/hsync-server:%s\n' "${VERSION}"
    printf 'Endpoint: https://%s:%s\n' "${endpoint_host}" "${PUBLIC_PORT}"

    # Snap-packaged docker is confined and cannot read hidden paths under $HOME,
    # so the default XDG install dir (~/.local/share/...) breaks 'docker compose'.
    if docker_is_snap && path_has_hidden_component "${INSTALL_DIR}"; then
        fail "docker is the snap package and ${INSTALL_DIR} is under a hidden path; the snap 'home' interface denies hidden paths in \$HOME, so 'docker compose' cannot read .env there. Re-run with a non-hidden --install-dir (e.g. --install-dir \"${HOME}/hsync-server\"), or install Docker Engine from https://docs.docker.com/engine/install/ instead of the snap."
    fi

    if [[ "${DRY_RUN}" == true ]]; then
        printf 'Dry run: no files were changed.\n'
        exit 0
    fi

    require_command docker
    require_command openssl
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"

    umask 077
    install_files
    docker compose --project-directory "${INSTALL_DIR}" \
        --file "${INSTALL_DIR}/compose.yaml" config --quiet

    if [[ "${START_SERVER}" == true ]]; then
        docker compose --project-directory "${INSTALL_DIR}" \
            --file "${INSTALL_DIR}/compose.yaml" pull
        docker compose --project-directory "${INSTALL_DIR}" \
            --file "${INSTALL_DIR}/compose.yaml" up -d --wait --wait-timeout 60
        printf 'HSYNC Server is healthy at https://%s:%s\n' "${endpoint_host}" "${PUBLIC_PORT}"
        printf 'The self-signed certificate is intended for private LAN use only.\n'
    else
        printf 'Configuration validated. Start it with:\n'
        printf '  docker compose --project-directory %q --file %q up -d --wait\n' \
            "${INSTALL_DIR}" "${INSTALL_DIR}/compose.yaml"
    fi
}

main "$@"
