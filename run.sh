#!/usr/bin/env bash

# HSYNC server — run from a fresh clone.
#
# Requires the hsync-common repository checked out as a sibling (the project
# declares it as a Poetry path dependency). This script is the uniform
# "run after clone" entry point, mirroring hsync-bootnode/run.sh: it creates (or
# reuses) the in-project virtualenv, installs the main dependencies once, and
# starts the server, forwarding any arguments to `hsync-server`.

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SIBLING="${SCRIPT_DIR}/../hsync-common"
readonly BIN="${SCRIPT_DIR}/.venv/bin/hsync-server"

if [[ ! -d "${SIBLING}" ]]; then
    printf 'Error: hsync-common must be checked out as a sibling of hsync-server.\n' >&2
    printf '  git clone <hsync-common-url> %s\n' "${SIBLING}" >&2
    exit 1
fi

if [[ ! -x "${BIN}" ]]; then
    command -v poetry >/dev/null 2>&1 || {
        printf 'Error: poetry is required to create the virtualenv.\n' >&2
        printf '  Install it (https://python-poetry.org/docs/#installation) or build the\n' >&2
        printf '  in-project venv yourself: poetry install --only main\n' >&2
        exit 1
    }
    printf 'Creating the in-project virtualenv (first run)…\n'
    cd "${SCRIPT_DIR}"
    poetry config virtualenvs.in-project true
    poetry install --only main --no-interaction
fi

exec "${BIN}" "$@"
