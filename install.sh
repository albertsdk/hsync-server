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
INSTALL_DIR_EXPLICIT=false
START_SERVER=true
DRY_RUN=false

usage() {
    cat <<EOF
Install HSYNC Server with Docker Compose.

Usage: ./install.sh [options]

When installing from the release download, forward options to bash (not curl):
  curl -fsSL "https://github.com/${REPO}/releases/latest/download/install.sh" | bash -s -- [options]

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
    local name="$1"
    if ! command -v "${name}" >/dev/null 2>&1; then
        print_tool_hint "${name}"
        fail "required command not found: ${name}"
    fi
}

# --- Prerequisite guidance --------------------------------------------------
#
# The installer never installs Docker and never runs sudo: when a prerequisite
# is missing it prints the commands and URLs for the user to run themselves, then
# fails with the same error line as before. Guidance is best-effort per platform
# and always backed by the canonical Docker documentation links.

# linux | macos | windows-shell | unknown
detect_platform_os() {
    case "$(uname -s)" in
        Linux) printf 'linux' ;;
        Darwin) printf 'macos' ;;
        MINGW* | MSYS* | CYGWIN* | Windows_NT) printf 'windows-shell' ;;
        *) printf 'unknown' ;;
    esac
}

# debian | rhel | arch | suse | alpine | unknown
detect_linux_family() {
    local id="" id_like="" key value
    if [[ -r /etc/os-release ]]; then
        while IFS='=' read -r key value; do
            value="${value%\"}"
            value="${value#\"}"
            case "${key}" in
                ID) id="${value}" ;;
                ID_LIKE) id_like="${value}" ;;
            esac
        done < /etc/os-release
    fi
    case " ${id} ${id_like} " in
        *" debian "* | *" ubuntu "*) printf 'debian' ;;
        *" rhel "* | *" fedora "* | *" centos "*) printf 'rhel' ;;
        *" arch "*) printf 'arch' ;;
        *" suse "* | *" opensuse "*) printf 'suse' ;;
        *" alpine "*) printf 'alpine' ;;
        *) printf 'unknown' ;;
    esac
}

print_docker_docs() {
    printf '  Docs: https://docs.docker.com/engine/install/\n' >&2
    printf '        https://docs.docker.com/compose/install/\n' >&2
    printf '        https://docs.docker.com/engine/install/linux-postinstall/\n' >&2
}

print_docker_install_hint() {
    local os family
    os="$(detect_platform_os)"
    family="$(detect_linux_family)"
    printf '\nDocker Engine (or Docker Desktop) with the Compose v2 plugin is required.\n' >&2
    case "${os}" in
        macos)
            printf 'Install Docker Desktop:\n' >&2
            printf '  brew install --cask docker\n' >&2
            printf 'Or a lightweight alternative:\n' >&2
            printf '  brew install colima docker docker-compose && colima start\n' >&2
            ;;
        windows-shell)
            printf 'Use Docker Desktop for Windows with the WSL2 backend and enable WSL\n' >&2
            printf 'integration for this distribution:\n' >&2
            printf '  https://docs.docker.com/desktop/install/windows-install/\n' >&2
            ;;
        linux)
            case "${family}" in
                debian)
                    printf 'Example (Debian/Ubuntu):\n' >&2
                    printf '  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2\n' >&2
                    printf 'Or the official convenience script: https://get.docker.com\n' >&2
                    ;;
                rhel)
                    printf 'Example (Fedora/RHEL/CentOS/Rocky):\n' >&2
                    printf '  sudo dnf install -y docker docker-compose-plugin\n' >&2
                    ;;
                arch)
                    printf 'Example (Arch):\n' >&2
                    printf '  sudo pacman -S --needed docker docker-compose\n' >&2
                    ;;
                suse)
                    printf 'Example (openSUSE):\n' >&2
                    printf '  sudo zypper install docker docker-compose\n' >&2
                    ;;
                alpine)
                    printf 'Example (Alpine):\n' >&2
                    printf '  sudo apk add docker docker-cli-compose\n' >&2
                    printf '  sudo rc-update add docker && sudo service docker start\n' >&2
                    ;;
                *)
                    printf 'Install Docker Engine and the Compose v2 plugin with your package manager.\n' >&2
                    ;;
            esac
            if [[ "${family}" != "alpine" ]]; then
                printf 'Then enable the service and add your user to the docker group:\n' >&2
                printf '  sudo systemctl enable --now docker\n' >&2
                printf '  sudo usermod -aG docker "$USER"   # then log out and back in\n' >&2
            fi
            ;;
        *)
            printf 'Install Docker Engine and the Compose v2 plugin for your system.\n' >&2
            ;;
    esac
    print_docker_docs
    case "${os}" in
        linux)
            printf 'Note: the snap-packaged Docker works, but its confined home interface\n' >&2
            printf 'cannot read hidden paths under $HOME; Docker Engine is recommended.\n' >&2
            printf 'Prefer not to run Docker? Install the Debian package or run from source:\n' >&2
            printf '  docs/getting-started-deb.md  docs/getting-started-source.md\n' >&2
            ;;
        macos)
            printf 'Prefer not to run Docker? Run from source:\n' >&2
            printf '  docs/getting-started-source.md\n' >&2
            ;;
    esac
}

print_docker_compose_hint() {
    local os family
    os="$(detect_platform_os)"
    family="$(detect_linux_family)"
    printf '\nThe Docker Compose v2 plugin is required (the "docker compose" command).\n' >&2
    if command -v docker-compose >/dev/null 2>&1; then
        printf 'Found Docker Compose v1 (docker-compose); this installer needs the v2 plugin.\n' >&2
    fi
    case "${os}" in
        macos)
            printf 'Docker Desktop bundles Compose v2; with the CLI only:\n' >&2
            printf '  brew install docker-compose\n' >&2
            ;;
        windows-shell)
            printf 'Docker Desktop for Windows bundles Compose v2.\n' >&2
            ;;
        linux)
            case "${family}" in
                debian) printf '  sudo apt-get install -y docker-compose-v2   # or docker-compose-plugin from the Docker repo\n' >&2 ;;
                rhel) printf '  sudo dnf install -y docker-compose-plugin\n' >&2 ;;
                arch) printf '  sudo pacman -S --needed docker-compose\n' >&2 ;;
                suse) printf '  sudo zypper install docker-compose\n' >&2 ;;
                alpine) printf '  sudo apk add docker-cli-compose\n' >&2 ;;
                *) printf '  Install the Compose v2 plugin with your package manager.\n' >&2 ;;
            esac
            ;;
        *)
            printf 'Install the Compose v2 plugin for your system.\n' >&2
            ;;
    esac
    printf '  Docs: https://docs.docker.com/compose/install/\n' >&2
}

print_docker_daemon_hint() {
    local info_output="$1"
    local os
    os="$(detect_platform_os)"
    if [[ "${info_output}" == *"permission denied"* ]]; then
        printf '\nThe Docker daemon is reachable, but this user cannot access it.\n' >&2
        printf 'Add your user to the docker group, then log out and back in (or run newgrp docker):\n' >&2
        printf '  sudo usermod -aG docker "$USER"\n' >&2
        printf '  Docs: https://docs.docker.com/engine/install/linux-postinstall/\n' >&2
        return 0
    fi
    printf '\nCannot reach the Docker daemon (is it running?).\n' >&2
    case "${os}" in
        macos) printf 'Start Docker Desktop and wait until it reports "running".\n' >&2 ;;
        windows-shell) printf 'Start Docker Desktop for Windows.\n' >&2 ;;
        *) printf '  sudo systemctl enable --now docker   # or: sudo service docker start\n' >&2 ;;
    esac
    if [[ -n "${DOCKER_HOST:-}" ]]; then
        printf 'DOCKER_HOST=%s is set: verify it points to a reachable daemon.\n' "${DOCKER_HOST}" >&2
    fi
    printf 'To only generate the files without starting the container, re-run with --no-start.\n' >&2
}

print_package_hint() {
    local os family pkg
    os="$(detect_platform_os)"
    family="$(detect_linux_family)"
    pkg="$1"
    case "${os}" in
        macos) printf '  brew install %s\n' "${pkg}" >&2 ;;
        linux)
            case "${family}" in
                debian) printf '  sudo apt-get install -y %s\n' "${pkg}" >&2 ;;
                rhel) printf '  sudo dnf install -y %s\n' "${pkg}" >&2 ;;
                arch) printf '  sudo pacman -S --needed %s\n' "${pkg}" >&2 ;;
                suse) printf '  sudo zypper install %s\n' "${pkg}" >&2 ;;
                alpine) printf '  sudo apk add %s\n' "${pkg}" >&2 ;;
                *) printf '  Install %s with your package manager.\n' "${pkg}" >&2 ;;
            esac
            ;;
        *) printf '  Install %s with your package manager.\n' "${pkg}" >&2 ;;
    esac
}

print_tool_hint() {
    local tool="$1"
    case "${tool}" in
        cosign)
            printf '\ncosign verifies the container image signature (or pass --no-verify to skip it).\n' >&2
            printf '  Docs: https://docs.sigstore.dev/cosign/system_config/installation/\n' >&2
            ;;
        openssl | tar | curl)
            printf '\nInstall "%s".\n' "${tool}" >&2
            print_package_hint "${tool}"
            ;;
        *)
            printf '\nInstall "%s" with your package manager.\n' "${tool}" >&2
            ;;
    esac
}

require_docker_command() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    print_docker_install_hint
    fail "required command not found: docker"
}

require_docker_compose_v2() {
    if docker compose version >/dev/null 2>&1; then
        return 0
    fi
    print_docker_compose_hint
    fail "Docker Compose v2 is required"
}

require_docker_daemon() {
    local info_output
    local -a docker_info=(docker info)
    if command -v timeout >/dev/null 2>&1; then
        docker_info=(timeout 10 docker info)
    elif command -v gtimeout >/dev/null 2>&1; then
        docker_info=(gtimeout 10 docker info)
    fi
    if info_output="$("${docker_info[@]}" 2>&1)"; then
        return 0
    fi
    print_docker_daemon_hint "${info_output}"
    fail "cannot connect to the Docker daemon"
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

# Snap docker cannot read hidden paths under $HOME. When a compose failure
# carries that signature, append an actionable hint beyond the pre-flight check.
warn_if_snap_hidden_path() {
    local output="$1"
    if ! docker_is_snap || [[ "${output}" != *"permission denied"* ]]; then
        return 0
    fi
    printf '\nWarning: docker is the snap package and the snap "home" interface denies\n' >&2
    printf 'hidden paths under $HOME, so it cannot read %s/.env.\n' "${INSTALL_DIR}" >&2
    printf 'Fix: install Docker Engine (https://docs.docker.com/engine/install/) instead\n' >&2
    printf 'of the snap, or re-run with a non-hidden directory:\n' >&2
    printf '  curl -fsSL "https://github.com/%s/releases/latest/download/install.sh" | bash -s -- --install-dir "%s"\n' \
        "${REPO}" "${HOME}/hsync-server" >&2
}

# 'docker compose config' reads .env client-side, so it is the first command to
# fail when snap docker cannot reach the installation directory.
validate_compose_config() {
    local output
    if output="$(docker compose --project-directory "${INSTALL_DIR}" \
            --file "${INSTALL_DIR}/compose.yaml" config --quiet 2>&1)"; then
        return 0
    fi
    printf '%s\n' "${output}" >&2
    warn_if_snap_hidden_path "${output}"
    fail "docker compose could not validate ${INSTALL_DIR}/compose.yaml"
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
                INSTALL_DIR_EXPLICIT=true
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

    # Snap-packaged docker is confined and cannot read hidden paths under $HOME,
    # so the default XDG install dir (~/.local/share/...) would break
    # 'docker compose'. When the directory was not chosen explicitly, fall back
    # to a non-hidden default instead of failing the whole install.
    if docker_is_snap && path_has_hidden_component "${INSTALL_DIR}"; then
        if [[ "${INSTALL_DIR_EXPLICIT}" == true ]]; then
            fail "docker is the snap package and ${INSTALL_DIR} is under a hidden path; the snap 'home' interface denies hidden paths in \$HOME, so 'docker compose' cannot read .env there. Re-run with a non-hidden directory:
  curl -fsSL \"https://github.com/${REPO}/releases/latest/download/install.sh\" | bash -s -- --install-dir \"${HOME}/hsync-server\"
or install Docker Engine from https://docs.docker.com/engine/install/ instead of the snap."
        fi
        printf 'Note: snap Docker cannot read hidden paths under $HOME; using %s instead of %s.\n' \
            "${HOME}/hsync-server" "${INSTALL_DIR}" >&2
        INSTALL_DIR="${HOME}/hsync-server"
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

    if [[ "${DRY_RUN}" == true ]]; then
        printf 'Dry run: no files were changed.\n'
        exit 0
    fi

    require_docker_command
    require_command openssl
    require_docker_compose_v2
    # The daemon is only needed to start containers; --no-start stays client-side.
    if [[ "${START_SERVER}" == true ]]; then
        require_docker_daemon
    fi

    umask 077
    install_files
    validate_compose_config

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
