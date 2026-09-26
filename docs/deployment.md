# HSYNC Server — Deployment Guide

This guide covers how to deploy the HSYNC Server in production: Docker Compose
with automatic TLS, reverse proxy configuration, and running as a systemd service.

> For day-to-day operations (backup, upgrade, monitoring, performance) see the
> [Operations Guide](operations.md).
>
> For a private-LAN quick start, see [Path A — Docker](getting-started-docker.md)
> or [Path B — Native](getting-started-native.md).
> For source builds and other methods, see
> [Installation Alternatives](installation-alternatives.md).

---

## Table of Contents

- [System Requirements](#system-requirements)
- [Build from Source with `docker.sh`](#build-from-source-with-dockersh)
- [Docker Compose (recommended for production)](#docker-compose-recommended-for-production)
- [Reverse Proxy Configuration](#reverse-proxy-configuration)
- [Systemd Service (without Docker)](#systemd-service-without-docker)
- [Troubleshooting](#troubleshooting)

---

## System Requirements

### Minimum

| Resource | Value |
|----------|-------|
| CPU | 1 core (x86_64 or arm64) |
| RAM | 256 MB |
| Disk | 1 GB free (plus storage for your files) |
| OS | Linux (kernel ≥ 4.15), macOS, or Windows (WSL2) |

### Recommended

| Resource | Value |
|----------|-------|
| CPU | 2 cores |
| RAM | 1 GB |
| Disk | SSD, with at least 10% free space above your expected storage needs |


---

## Build from Source with `docker.sh`

The project includes a [`docker.sh`](../docker.sh) script for contributors and
source deployments. It handles image building, container lifecycle, data
directory management, and SSL configuration. It is not the release quick start; use
[Path A — Docker](getting-started-docker.md) unless you need a local build.

### Available commands

```bash
./docker.sh build       # Build the Docker image
./docker.sh start       # Start the container
./docker.sh stop        # Stop the container
./docker.sh restart     # Restart the container
./docker.sh status      # Show container status
./docker.sh logs        # Show container logs (use -f to follow)
./docker.sh shell       # Open a shell in the running container
./docker.sh clean       # Remove container, image, and the TLS key material (keeps data)
./docker.sh clean --all # Also drop the host data directory (ALL user data lost)
```

> **Data lives on the host in a single data directory.** By default
> `./docker.sh` keeps everything under `hsync-data/` next to the script
> (override with `HSYNC_DATA_DIR`): `buckets/` for your files, `state/` for
> the causal database and `certs/` for the TLS identity, bind-mounted at the
> canonical `/project/{buckets,state,certs}` paths inside the container.
> Backup is a plain copy of that directory, and migration to another host is
> a restore of the same directory. The container runs as the invoking user's
> UID/GID, so the bind-mounted directories are always writable without any
> manual `chown`.

### Build and start from source

```bash
# 1. Build the image (first time only, or after pulling changes)
./docker.sh build

# 2. Start the server with an environment file
./docker.sh start --env-file prod.env

# 3a. The script waits for the server to answer /health and prints the URL to use:
#     ✔ HSYNC Server is live → https://localhost:9753
#     ✔ API documentation: https://localhost:9753/docs   (only when HSYNC_ENABLE_DOCS=true)
#     ⚠ Self-signed certificate: use curl -k or accept the browser warning
#
# 3b. Optional manual check:
./docker.sh status

# 4. View logs (the startup log also prints "Serving at <url>" lines)
./docker.sh logs -f

# 5. Smoke test (self-signed cert, hence -k)
curl -k https://localhost:9753/health && echo "SERVER OK!"
```

> The URL scheme printed by `docker.sh start` is derived from the env-file:
> `HSYNC_SSL_ENABLED=true` (the default in both `dev.env` and `prod.env`)
> means **https**, otherwise http. To browse the interactive API docs,
> set `HSYNC_ENABLE_DOCS=true` in your env-file (it defaults to `false`
> and `prod.env` keeps it off).

### Container-aware behavior

At startup the server detects whether it runs inside a container and logs a
summary line with the container runtime, the cgroup memory/CPU limits and the
TTY state, e.g.:

```text
Runtime environment: container=docker (via /.dockerenv), mem_limit=512.0 MB, cpu_quota=1.5 cores, tty=no
```

Inside a container the sensible non-interactive defaults apply automatically:
colored output is disabled (no TTY; the standard
[`NO_COLOR`](https://no-color.org) variable is also honored) and logs go to
stdout for `docker logs` / `docker compose logs`. For log aggregation
pipelines, structured JSON output is available with `HSYNC_LOG_FORMAT=json`.

### Passing environment variables

The script accepts an `--env-file` option to pass a file with `HSYNC_*`
variables to the container:

```bash
./docker.sh start --env-file prod.env
```

You can also set variables directly in the shell environment. The script
forwards them to Docker by name (not by value), keeping secrets off the command
line:

```bash
export HSYNC_ADMIN_KEY="$(openssl rand -hex 32)"
export HSYNC_SSL_ENABLED=true
export HSYNC_SSL_KEYFILE_PASSWORD="$(openssl rand -hex 32)"
./docker.sh start
```

### SSL/TLS with `docker.sh`

Enable HTTPS/WSS by setting the relevant environment variables before starting:

```bash
HSYNC_SSL_ENABLED=true \
HSYNC_SSL_KEYFILE_PASSWORD="$(openssl rand -hex 32)" \
HSYNC_ADMIN_KEY=secret \
./docker.sh start
```

The script forwards all `HSYNC_SSL_*` variables to the container. See
[README → Environment Variables](README.md#environment-variables) for the
complete SSL variable list.

### Restart with the same configuration

`./docker.sh restart` remembers the `--env-file` used by the last `start`, so
you don't need to specify it again:

```bash
./docker.sh start --env-file prod.env
# ... make configuration changes to prod.env ...
./docker.sh restart   # reuses prod.env automatically
```

### Clean up

```bash
./docker.sh clean
```

This stops and removes the container, deletes the Docker image, and removes the
`certs/` subdirectory of the data directory (forcing a fresh self-signed
certificate on the next start). `buckets/` and `state/` are **kept**; pass
`--all` (`./docker.sh clean --all`) to delete the whole data directory together
with all user data.

---

## Docker Compose (recommended for production)

For production deployments that need automatic TLS via Let's Encrypt, use
Docker Compose with a reverse proxy. Below is a complete example with Caddy.

```yaml
# docker-compose.yml
services:
  hsync-server:
    image: ghcr.io/albertsdk/hsync-server:v1.0.0
    container_name: hsync-server
    restart: unless-stopped
    expose:
      - "9753"
    env_file:
      - ./prod.env
    user: "1000:1000"
    volumes:
      - ./hsync-data/buckets:/project/buckets
      - ./hsync-data/state:/project/state
      - ./hsync-data/certs:/project/certs
      - ./hsync-data/logs:/project/logs
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:9753/health", timeout=5)'
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - hsync-net

  caddy:
    image: caddy:2-alpine
    container_name: hsync-caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks:
      - hsync-net
    depends_on:
      - hsync-server

volumes:
  caddy-data:
  caddy-config:

networks:
  hsync-net:
    driver: bridge
```

### Caddyfile

```caddy
# Caddyfile
your-domain.com {
    reverse_proxy hsync-server:9753
}
```

Caddy automatically provisions and renews Let's Encrypt certificates. Replace
`your-domain.com` with your actual domain.

> **Note**: The container healthcheck above probes `http://127.0.0.1:9753/health`
> because the reverse proxy terminates TLS and the server listens on plain HTTP
> (`HSYNC_SSL_ENABLED=false`). If you instead enable the built-in TLS terminator
> (`HSYNC_SSL_ENABLED=true`), change the probe URL to
> `https://127.0.0.1:9753/health` and disable certificate verification for the
> self-signed certificate (e.g. `urllib.request` with an unverified SSL context),
> otherwise the healthcheck will fail.

### Environment file (`prod.env`)

```dotenv
# prod.env — generate the admin secret with: openssl rand -hex 32
HSYNC_ADMIN_KEY=ADMIN_SECRET_64_HEX
HSYNC_LOG_LEVEL=info
HSYNC_LOG_FORMAT=json
HSYNC_LOG_FILE=/project/logs/hsync-server.log
HSYNC_LOG_ROTATION=100 MB
HSYNC_LOG_RETENTION=30 days
HSYNC_TZ=UTC
HSYNC_BUCKETS=/project/buckets
HSYNC_STATE_DIR=/project/state
HSYNC_SSL_ENABLED=false
HSYNC_MAX_UPLOAD_SIZE=10737418240
HSYNC_MIN_FREE_DISK_SPACE=524288000
HSYNC_TOMBSTONE_TTL_DAYS=30
HSYNC_TRUSTED_PROXIES=172.16.0.0/12
HSYNC_RATE_LIMIT_DEFAULT=500/minute
HSYNC_RATE_LIMIT_UPLOAD=200/minute
HSYNC_RATE_LIMIT_DOWNLOAD=1000/minute
HSYNC_RATE_LIMIT_ADMIN=20/minute
HSYNC_RATE_LIMIT_WS_MESSAGES=10/second
HSYNC_WS_MAX_CONNECTIONS=1000
HSYNC_WS_MAX_CONNECTIONS_PER_IP=10
```

> **Important**: Generate a strong random admin key (64 hex):
> ```bash
> openssl rand -hex 32
> ```

### Start the stack

```bash
docker compose up -d
```

---

## Reverse Proxy Configuration

The HSYNC Server does **not** include a built-in TLS terminator for production.
Always run it behind a reverse proxy that terminates HTTPS.

See the [Reverse Proxy Guide](reverse-proxy.md) for Nginx and Traefik
configuration examples. Caddy is covered in the
[Docker Compose](#docker-compose-recommended-for-production) section above.

---

## Systemd Service (without Docker)

If you prefer to run the server directly (without Docker), you can install it
via Poetry and manage it with systemd.

### Install

```bash
# Create the service account and sibling checkout layout
sudo useradd --system --home /opt/hsync --shell /usr/sbin/nologin hsync
sudo install -d -o hsync -g hsync /opt/hsync
sudo -u hsync git clone https://github.com/albertsdk/hsync-server.git /opt/hsync/hsync-server
sudo -u hsync git clone https://github.com/albertsdk/hsync-common.git /opt/hsync/hsync-common

# Install dependencies
cd /opt/hsync/hsync-server
sudo -u hsync env POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main

# Create required directories
sudo -u hsync mkdir -p /opt/hsync/{buckets,state,certs,logs}
```

Create `/opt/hsync/prod.env` with at least a strong `HSYNC_ADMIN_KEY` and these
host paths. If a reverse proxy terminates TLS, keep built-in SSL disabled:

```dotenv
# Generate the admin secret with: openssl rand -hex 32
HSYNC_ADMIN_KEY=ADMIN_SECRET_64_HEX
HSYNC_BUCKETS=/opt/hsync/buckets
HSYNC_STATE_DIR=/opt/hsync/state
HSYNC_SSL_CERT_DIR=/opt/hsync/certs
HSYNC_LOG_FILE=/opt/hsync/logs/hsync-server.log
HSYNC_SSL_ENABLED=false
```

Protect it with `sudo chown hsync:hsync /opt/hsync/prod.env` and
`sudo chmod 600 /opt/hsync/prod.env`.

### Systemd unit

```ini
# /etc/systemd/system/hsync-server.service
[Unit]
Description=HSYNC Server
After=network.target

[Service]
Type=simple
User=hsync
Group=hsync
WorkingDirectory=/opt/hsync/hsync-server
EnvironmentFile=/opt/hsync/prod.env
ExecStart=/opt/hsync/hsync-server/.venv/bin/hsync-server
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/hsync/buckets /opt/hsync/certs /opt/hsync/logs

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable hsync-server
sudo systemctl start hsync-server
sudo systemctl status hsync-server
```

---

## Troubleshooting

### Container won't start

**Port already in use:**
```bash
# Check what is using port 9753
sudo lsof -i :9753
# Or change the port with HSYNC_PORT in your env file
```

**Container exits immediately:**
```bash
# Check the container logs
./docker.sh logs
```
Common causes:
- `HSYNC_ADMIN_KEY` malformed — must be exactly 64 lowercase hex characters
  (generate with `openssl rand -hex 32`). When unset, the server generates a
  secret itself and prints the admin token once; it does not require the
  variable to start.
- `HSYNC_SSL_KEYFILE_PASSWORD` missing when `HSYNC_SSL_ENABLED=true`.
- Volume permissions — `./docker.sh` and the Compose template run the
  container as the invoking user's UID/GID (`HSYNC_UID`/`HSYNC_GID`), so the
  bind-mounted `hsync-data/` directories stay writable. If you mount custom
  host directories manually, make sure they are writable by that user.

**SSL keyfile password missing:**
```
Error: HSYNC_SSL_KEYFILE_PASSWORD is required when SSL is enabled
```
Set `HSYNC_SSL_KEYFILE_PASSWORD` when `HSYNC_SSL_ENABLED=true`.

### Clients cannot connect

**Connection refused:**
```bash
# Verify the server is listening (use -k for the self-signed certificate
# when HSYNC_SSL_ENABLED=true, as in the shipped env-file profiles)
curl -k https://localhost:9753/health && echo "SERVER OK!"

# Check firewall rules
sudo ufw status
# Open the port if needed
sudo ufw allow 9753/tcp
```

**TLS certificate error:**
- Ensure the reverse proxy is properly configured and the certificate is valid.
- Clients connecting with self-signed certificates need `--ca-cert` or `--insecure`.
  See the [Client Configuration — Server Mode → Server Connection Options](https://github.com/albertsdk/hsync-client/blob/main/docs/configuration-server.md#server-connection-options)
  guide.

**Authentication failure (`401` / WS close `4003`):**
- The client must present a valid account token (`hs_<id>_<secret>`) via its
  `HSYNC_API_KEY` environment variable. `HSYNC_ADMIN_KEY` is a server-side
  bootstrap secret only — never set it on clients.
- Issue tokens with `hsync-server admin client add --name <name> --role client`
  (see [Roles and Permissions → CLI Tools](roles-and-permissions.md#cli-tools)).
- Check the server logs for authentication failures.

**Wrong WebSocket endpoint:**
- The WebSocket URL is `wss://host:9753/api/v1/buckets/{bucket}/events/{cid}`.
- The client must send an `auth` frame as the first message.

### Where to find server logs

| Deployment | Log location |
|------------|-------------|
| Docker (via `docker.sh`) | `./docker.sh logs` or `./docker.sh logs -f` |
| Docker Compose | `docker compose logs hsync-server` |
| Systemd | `journalctl -u hsync-server` |
| Direct (Poetry) | stdout/stderr, or the configured log file |

### Debug mode

Enable debug logging for more verbose output:

```bash
HSYNC_LOG_LEVEL=debug ./docker.sh start
```

Or set `HSYNC_LOG_LEVEL=debug` in your environment file.

---

## See also

- [Operations Guide](operations.md) — backup, upgrade, monitoring, performance.
- [Getting Started](getting-started.md) — chooser between the Docker and native
  install paths.
- [Installation Alternatives](installation-alternatives.md) — source and manual installs.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [Architecture](internal/architecture.md) — server components and module structure.
- [Reverse Proxy Guide](reverse-proxy.md) — nginx and Traefik configuration.
- [Sync Flow](sync-flow.md) — end-to-end synchronization behavior.
- [FAQ](faq.md) — frequently asked questions.
- [Client Examples](https://github.com/albertsdk/hsync-client/blob/main/docs/examples.md) — usage examples for the HSYNC client.
