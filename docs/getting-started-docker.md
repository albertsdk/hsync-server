# Getting Started — Method 2: Docker / Compose (manual)

Use this method when you already manage containers, want to run the released
image with your own Compose setup, or want to build the image from source. For
the one-command installer instead, see
[Method 1: installer script](getting-started-script.md).

> The quick start uses a persistent self-signed certificate: suitable for a
> trusted private LAN, not for exposing HSYNC directly to the Internet. For a
> public server, follow the [Production Deployment Guide](deployment.md).

## Prerequisites

- <img src="assets/os/linux.svg" alt="Linux" width="16" height="16"> <img src="assets/os/apple.svg" alt="macOS" width="16" height="16"> Linux or macOS with Docker Engine or Docker Desktop running
- Docker Compose v2 (`docker compose version`)
- OpenSSL (to generate the admin secret)

## Install

### With the released image (manual Compose)

Download `compose.yaml` from the release, create a private `.env` beside it, and
start it:

```dotenv
HSYNC_IMAGE=ghcr.io/albertsdk/hsync-server:v1.0.0
HSYNC_BIND_ADDRESS=192.168.1.20
HSYNC_CERT_HOSTNAME=192.168.1.20
# Generate the admin secret with: openssl rand -hex 32
HSYNC_ADMIN_KEY=ADMIN_SECRET_64_HEX
HSYNC_SSL_ENABLED=true
```

Protect and validate the configuration, then start it:

```bash
chmod 600 .env compose.yaml
docker compose config --quiet
docker compose pull
docker compose up -d --wait
```

Use the release `SHA256SUMS` file to verify `compose.yaml`. Do not mix a Compose
file from one release with an image from another without reviewing the release
notes.

### Quick container (diagnostic only)

A temporary `docker run` is useful only for a short image or health-check
diagnostic — it has no declarative lifecycle or persistent TLS identity:

```bash
docker run --rm -d --name hsync-server-test \
  -p 127.0.0.1:9753:9753 \
  -e HSYNC_ADMIN_KEY="$(openssl rand -hex 32)" \
  ghcr.io/albertsdk/hsync-server:v1.0.0
curl http://127.0.0.1:9753/health
docker stop hsync-server-test
```

The diagnostic endpoint is plaintext and bound to localhost. HSYNC clients
refuse to send API keys over HTTP, so do not use this as a deployment.

### Build the image from source

The source build requires `hsync-server` and `hsync-common` as sibling
repositories. From the `hsync-server` checkout:

```bash
cp dev.env.example dev.env
./docker.sh build
./docker.sh start --env-file dev.env
```

`docker.sh start` starts an image that has already been built; it does not pull
the GHCR image or build automatically. `./docker.sh` also supports `stop`,
`restart`, `status`, `logs -f`, `shell` and `clean`. See the
[Developer Guide](internal/developer.md) for the repository setup.

## Verify the server is healthy

```bash
curl --insecure https://localhost:9753/health && echo "SERVER OK!"
```

With the manual Compose setup, the certificate lives under
`<data-dir>/certs/server.crt`; export it and connect clients exactly as in
[Method 1 → Export the certificate](getting-started-script.md#export-the-certificate-for-clients).
The admin CLI is `docker compose exec server …` in both Docker methods — see
[Roles and Permissions](roles-and-permissions.md).

## Manage and update

```bash
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
docker compose up -d --wait
```

`docker compose down` does not delete the data directory. To update, pull the
new image tag (or rebuild with `./docker.sh build`) and bring the stack up
again. Review the release notes and back up the data directory first. See
[Operations → Upgrade](operations.md#upgrade-guide).

## Next

- Prefer the automated installer? [Installer script](getting-started-script.md).
- No Docker host? [Debian package](getting-started-deb.md) or
  [run from source](getting-started-source.md).
