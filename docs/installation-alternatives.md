# HSYNC Server — Installation Alternatives

Use the [recommended quick start](getting-started.md) unless one of the cases
below applies. These procedures are intentionally separate so development,
diagnostic, and production assumptions are not mixed into the normal install.

## Choose a method

| Scenario | Method |
| --- | --- |
| Private LAN, least setup | [Verified Compose bootstrap](getting-started.md) |
| Public Internet server | [Production Deployment Guide](deployment.md) |
| Existing Compose automation | [Manual Compose](#manual-compose) |
| Temporary image diagnostic | [Direct Docker run](#direct-docker-run) |
| Modify or contribute code | [Build from source](#build-from-source) |
| Host where Docker is unavailable | [Poetry and systemd](#poetry-and-systemd) |

## Manual Compose

Download `compose.yaml` from the same release as the image and create a private
`.env` beside it. The required values are:

```dotenv
HSYNC_IMAGE=ghcr.io/albertobasso/hsync-server:v1.0.0
HSYNC_BIND_ADDRESS=192.168.1.20
HSYNC_CERT_HOSTNAME=192.168.1.20
HSYNC_PUBLIC_PORT=9753
HSYNC_API_KEY=<strong-random-secret>
HSYNC_SSL_ENABLED=true
HSYNC_SSL_KEYFILE_PASSWORD=<different-strong-random-secret>
HSYNC_BUCKETS=/project/buckets
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
  -e HSYNC_API_KEY=<temporary-random-key> \
  ghcr.io/albertobasso/hsync-server:v1.0.0
curl http://127.0.0.1:9753/health
docker stop hsync-server-test
```

The diagnostic endpoint is plaintext and bound to localhost. HSYNC clients
refuse to send API keys over HTTP, so do not use this as a client deployment or
expose it to a network.

## Build from source

The source Docker build requires `hsync-server` and `hsync-common` as sibling
repositories. From the `hsync-server` checkout:

```bash
./docker.sh build
./docker.sh start --env-file dev.env
```

`docker.sh start` starts an image that has already been built; it does not pull
the GHCR image or build automatically. See the
[Developer Guide](developer.md) for the repository setup.

## Poetry and systemd

Use Poetry for development or when the host cannot run Docker:

```bash
poetry install --only main
HSYNC_API_KEY=<strong-random-secret> poetry run hsync-server
```

Python 3.12 or newer and the sibling `hsync-common` dependency are required. For
a managed host service, follow the [systemd deployment](deployment.md#systemd-service-without-docker)
instead of running the command in an interactive shell.
