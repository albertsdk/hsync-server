# HSYNC Server — Deployment Guide

This guide covers how to deploy the HSYNC Server in production: Docker Compose
with automatic TLS, reverse proxy configuration, and running as a systemd service.

> For day-to-day operations (backup, upgrade, monitoring, performance) see the
> [Operations Guide](operations.md).
>
> For a quick start (single container, development, or LAN testing) see the
> [README → Install & quickstart](README.md#install--quickstart).

---

## Table of Contents

- [System Requirements](#system-requirements)
- [Docker Management with `docker.sh`](#docker-management-with-dockersh)
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

## Docker Management with `docker.sh`

The project includes a [`docker.sh`](../docker.sh) script that simplifies all
Docker operations. It handles image building, container lifecycle, volume
management, and SSL configuration — without needing to remember raw `docker`
commands.

### Available commands

```bash
./docker.sh build       # Build the Docker image
./docker.sh start       # Start the container
./docker.sh stop        # Stop the container
./docker.sh restart     # Restart the container
./docker.sh status      # Show container status
./docker.sh logs        # Show container logs (use -f to follow)
./docker.sh shell       # Open a shell in the running container
./docker.sh clean       # Remove container, image, and SSL certificate volume
```

### Quick start with `docker.sh`

```bash
# 1. Build the image (first time only, or after pulling changes)
./docker.sh build

# 2. Start the server with an environment file
./docker.sh start --env-file prod.env

# 3. Check that it's running
./docker.sh status

# 4. View logs
./docker.sh logs -f
```

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
export HSYNC_API_KEY="$(openssl rand -base64 32)"
export HSYNC_SSL_ENABLED=true
export HSYNC_SSL_KEYFILE_PASSWORD="$(openssl rand -base64 32)"
./docker.sh start
```

### SSL/TLS with `docker.sh`

Enable HTTPS/WSS by setting the relevant environment variables before starting:

```bash
HSYNC_SSL_ENABLED=true \
HSYNC_SSL_KEYFILE_PASSWORD="$(openssl rand -base64 32)" \
HSYNC_API_KEY=secret \
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
SSL certificate volume (forcing a fresh self-signed certificate on the next
start).

---

## Docker Compose (recommended for production)

For production deployments that need automatic TLS via Let's Encrypt, use
Docker Compose with a reverse proxy. Below is a complete example with Caddy.

```yaml
# docker-compose.yml
services:
  hsync-server:
    image: ghcr.io/albertobasso/hsync-server:latest
    container_name: hsync-server
    restart: unless-stopped
    expose:
      - "8765"
    env_file:
      - ./prod.env
    volumes:
      - hsync-buckets:/project/buckets
      - hsync-certs:/project/certs
      - hsync-logs:/project/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8765/health"]
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
  hsync-buckets:
  hsync-certs:
  hsync-logs:
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
    reverse_proxy hsync-server:8765
}
```

Caddy automatically provisions and renews Let's Encrypt certificates. Replace
`your-domain.com` with your actual domain.

### Environment file (`prod.env`)

```bash
# prod.env
HSYNC_API_KEY=<generate-a-strong-random-key>
HSYNC_LOG_LEVEL=info
HSYNC_LOG_FORMAT=json
HSYNC_LOG_FILE=/project/logs/hsync-server.log
HSYNC_LOG_ROTATION=100 MB
HSYNC_LOG_RETENTION=30 days
HSYNC_TZ=UTC
HSYNC_BUCKETS=/project/buckets
HSYNC_MAX_UPLOAD_SIZE=10737418240
HSYNC_MIN_FREE_DISK_SPACE=524288000
HSYNC_TOMBSTONE_TTL_DAYS=30
HSYNC_TRUSTED_PROXIES=172.16.0.0/12
HSYNC_RATE_LIMIT_DEFAULT=500/minute
HSYNC_RATE_LIMIT_UPLOAD=200/minute
HSYNC_RATE_LIMIT_DOWNLOAD=1000/minute
HSYNC_RATE_LIMIT_WEBSOCKET=50/minute
HSYNC_RATE_LIMIT_WS_MESSAGES=10/second
HSYNC_WS_MAX_CONNECTIONS=1000
HSYNC_WS_MAX_CONNECTIONS_PER_IP=10
```

> **Important**: Generate a strong random API key:
> ```bash
> openssl rand -base64 32
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
# Clone the repository
git clone https://github.com/albertobasso/hsync-server.git
cd hsync-server

# Install dependencies
poetry install --only main

# Create required directories
sudo mkdir -p /opt/hsync-server/{buckets,certs,logs}
sudo chown -R hsync:hsync /opt/hsync-server
```

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
WorkingDirectory=/opt/hsync-server
EnvironmentFile=/opt/hsync-server/prod.env
ExecStart=/usr/local/bin/poetry run hsync-server
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/hsync-server/buckets /opt/hsync-server/certs /opt/hsync-server/logs

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
# Check what is using port 8765
sudo lsof -i :8765
# Or change the port with HSYNC_PORT in your env file
```

**Container exits immediately:**
```bash
# Check the container logs
./docker.sh logs
```
Common causes:
- `HSYNC_API_KEY` not set — the server requires an API key.
- `HSYNC_SSL_KEYFILE_PASSWORD` missing when `HSYNC_SSL_ENABLED=true`.
- Volume permissions — the container user (UID 1000) must be able to write to
  the buckets and certs directories.

**SSL keyfile password missing:**
```
Error: HSYNC_SSL_KEYFILE_PASSWORD is required when SSL is enabled
```
Set `HSYNC_SSL_KEYFILE_PASSWORD` when `HSYNC_SSL_ENABLED=true`.

### Clients cannot connect

**Connection refused:**
```bash
# Verify the server is listening
curl http://localhost:8765/health

# Check firewall rules
sudo ufw status
# Open the port if needed
sudo ufw allow 8765/tcp
```

**TLS certificate error:**
- Ensure the reverse proxy is properly configured and the certificate is valid.
- Clients connecting with self-signed certificates need `--ca-cert` or `--insecure`.
  See the [Client Configuration → SSL/TLS](https://github.com/albertobasso/hsync-client/blob/main/docs/configuration.md#ssltls-httpswss)
  guide.

**API key mismatch:**
- Verify that the client is using the same `HSYNC_API_KEY` as the server.
- Check the server logs for authentication failures.

**Wrong WebSocket endpoint:**
- The WebSocket URL is `wss://host:8765/api/v1/buckets/{bucket}/events/{cid}`.
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
- [README](README.md) — install and quickstart.
- [API Reference](api.md) — complete REST + WebSocket contract.
- [Architecture](architecture.md) — server components and module structure.
- [Reverse Proxy Guide](reverse-proxy.md) — nginx and Traefik configuration.
- [Sync Flow](sync-flow.md) — end-to-end synchronization behavior.
- [FAQ](faq.md) — frequently asked questions.
- [Client Examples](https://github.com/albertobasso/hsync-client/blob/main/docs/examples.md) — usage examples for the HSYNC client.
