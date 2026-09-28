# HSYNC Server — Installation Alternatives

Use one of the two [Getting Started paths](getting-started.md) unless one of
the cases below applies. These procedures are intentionally separate so
development, diagnostic, and production assumptions are not mixed into the
normal install.

## Choose a method

| Scenario | Method |
| --- | --- |
| Private LAN, least setup | [Path A — Docker](getting-started-docker.md) |
| No Docker / native systemd host | [Path B — Native](getting-started-native.md) |
| Public Internet server | [Production Deployment Guide](deployment.md) |
| Existing Compose automation | [Manual Compose](#manual-compose) |
| Temporary image diagnostic | [Direct Docker run](#direct-docker-run) |
| Standalone executable, no Docker | [Standalone binary](#standalone-binary) |
| Packaged native install (Debian/Ubuntu) | [Debian/Ubuntu package](#debianubuntu-package) |
| Modify or contribute code | [Build from source](#build-from-source) |
| Host where Docker is unavailable | [Path B — Native](getting-started-native.md) |

## Manual Compose

Download `compose.yaml` from the same release as the image and create a private
`.env` beside it. The required values are:

```dotenv
HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v1.0.0
HSYNC_BIND_ADDRESS=192.168.1.20
HSYNC_CERT_HOSTNAME=192.168.1.20
HSYNC_PUBLIC_PORT=9753
# Generate the admin secret with: openssl rand -hex 32
HSYNC_ADMIN_KEY=ADMIN_SECRET_64_HEX
HSYNC_SSL_ENABLED=true
# Host data directory bind-mounted at /project/{buckets,state,certs}.
HSYNC_DATA_DIR=./hsync-data
HSYNC_UID=1000
HSYNC_GID=1000
HSYNC_BUCKETS=/project/buckets
HSYNC_STATE_DIR=/project/state
HSYNC_SSL_CERT_DIR=/project/certs
```

Protect and validate the configuration before starting it:

```bash
chmod 600 .env compose.yaml
docker compose config --quiet
docker compose pull
docker compose up -d --wait
```

Use the release `SHA256SUMS` file to verify `compose.yaml`. Do not use a Compose
file from one release with an image from another release without reviewing the
release notes.

## Direct Docker run

This method is useful only for a short image or health-check diagnostic. It does
not provide the declarative lifecycle and persistent TLS identity of the normal
installation:

```bash
docker run --rm -d --name hsync-server-test \
  -p 127.0.0.1:9753:9753 \
  -e HSYNC_ADMIN_KEY="$(openssl rand -hex 32)" \
  ghcr.io/albertsdk/hsync-server:v1.0.0
curl http://127.0.0.1:9753/health
docker stop hsync-server-test
```

The diagnostic endpoint is plaintext and bound to localhost. HSYNC clients
refuse to send API keys over HTTP, so do not use this as a client deployment or
expose it to a network.

## Standalone binary

`compile.sh` freezes the server into a self-contained executable that runs
without a Python installation. From the `hsync-server` checkout, install the
build group once and build for the host:

```bash
poetry install --with build
./compile.sh
# output: dist/<os>-<arch>/hsync-server
./dist/linux-amd64/hsync-server --version
```

The executable reads the same `HSYNC_*` environment variables and CLI options
as the native install (`./compile.sh --help` lists the options, including
`--clean`). It is a good fit for a host where Docker is unavailable and no
managed Python environment is desired.

### Raspberry Pi OS executables

Executables for Raspberry Pi OS 32-bit (`armhf`) and 64-bit (`arm64`) are
cross-built inside Docker, so no host Python is needed:

```bash
./compile.sh --target rpi
```

Output: `dist/rpi/armhf/hsync-server` and `dist/rpi/arm64/hsync-server`. The
host needs Docker with `qemu-user-static` binfmt handlers configured for
cross-architecture emulation.

### Debian/Ubuntu package

The `deb` and `deb-all` targets assemble a `.deb` that installs the frozen
server under `/usr/lib/hsync-server` (with `/usr/bin/hsync-server` on PATH), a
`hsync-server.service` systemd unit, and a conffile at
`/etc/hsync-server/hsync-server.conf`.

```bash
poetry install --with build
./compile.sh --target deb
# output: dist/deb/hsync-server_<ver>_<arch>.deb
sudo dpkg -i dist/deb/hsync-server_<ver>_<arch>.deb
sudo apt install -f
```

Set `HSYNC_ADMIN_KEY` in `/etc/hsync-server/hsync-server.conf`
(`openssl rand -hex 32`), then opt in:

```bash
sudo systemctl enable --now hsync-server.service
```

Data is stored under `/var/lib/hsync-server`; logs under
`/var/log/hsync-server`. The package creates a dedicated `_hsync-server` system
user and never enables the service automatically.

`./compile.sh --target deb-all` cross-builds the `amd64`, `arm64` and `armhf`
packages inside Docker.

## Build from source

The source Docker build requires `hsync-server` and `hsync-common` as sibling
repositories. From the `hsync-server` checkout:

```bash
./docker.sh build
./docker.sh start --env-file dev.env
```

`docker.sh start` starts an image that has already been built; it does not pull
the GHCR image or build automatically. See the
[Developer Guide](internal/developer.md) for the repository setup.

## Poetry and systemd

The native quick start (installation, first start, client connection) is
covered end-to-end in [Path B — Native](getting-started-native.md). Here only
the minimum to launch for development:

```bash
poetry install --only main
HSYNC_ADMIN_KEY="$(openssl rand -hex 32)" \
poetry run hsync-server --ssl --ssl-cert-dir ./certs
```

Python 3.12 or newer and the sibling `hsync-common` dependency are required. For
a managed host service, follow the [systemd deployment](deployment.md#systemd-service-without-docker)
instead of running the command in an interactive shell.
