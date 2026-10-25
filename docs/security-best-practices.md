# HSYNC Server — Security Best Practices

This page covers hardening recommendations for production HSYNC Server
deployments. Follow these guidelines to minimize the attack surface and protect
your data.

> The [SECURITY.md](../SECURITY.md) file covers supported versions and how to
> report vulnerabilities.

## Table of Contents

- [Account and Key Management](#account-and-key-management)
- [Network Exposure](#network-exposure)
- [TLS Configuration](#tls-configuration)
- [Filesystem Permissions](#filesystem-permissions)
- [Rate Limiting](#rate-limiting)
- [WebSocket Hardening](#websocket-hardening)
- [Logging & Auditing](#logging--auditing)
- [Checklist](#checklist)

---

## Account and Key Management

HSYNC authenticates clients with per-account tokens (`hs_<id>_<secret>`), each
with a role and bucket memberships. See
[Roles and Permissions](roles-and-permissions.md) for the full model.

### Generate a strong admin secret

The bootstrap admin secret (`HSYNC_ADMIN_KEY`) must be exactly 64 lowercase hex
characters:

```bash
openssl rand -hex 32
# → 3f9c2b7a... (64 hex chars)
```

### Rotate keys

Rotate an account's key periodically (e.g., every 90 days) and immediately if a
key is suspected compromised. Rotation is built in and does not require a
server restart:

```bash
hsync-server admin client rotate-key \
  --name ACCOUNT --grace 3600
```

The `--grace` window keeps the old key valid briefly so clients can migrate
without a hard disconnect. Disable an account immediately with
`hsync-server admin client disable --name ACCOUNT` to revoke access outright.

### Never expose tokens or secrets

- Store `HSYNC_ADMIN_KEY` **only** in `prod.env`, never in shell history or
  version control
- Start from the committed `dev.env.example` / `prod.env.example` templates:
  the local `dev.env` / `prod.env` are gitignored, so secrets never enter
  version control
- Use environment variable injection from a secrets manager (Vault, SOPS, etc.)
  in automated deployments

---

## Network Exposure

### Only expose ports 80 and 443

The HSYNC Server itself listens on port `9753` by default. **Never expose this
port directly to the internet.** Instead:

```text
Internet ──► Reverse Proxy (:443) ──► HSYNC Server (:9753, localhost only)
```

In Docker Compose, use `expose` (not `ports`) for the hsync-server service:

```yaml
hsync-server:
  expose:
    - "9753"           # internal only
  # Do NOT add:
  # ports:
  #   - "9753:9753"    # this exposes the port publicly
```

### Firewall rules

Minimal UFW rules for an Ubuntu/Debian server:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (redirect to HTTPS)
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable
```

> Port `9753` should **not** be opened. Only the reverse proxy (nginx, Caddy,
> Traefik) on ports 80/443 needs external access.

### Trusted proxies

If your reverse proxy is on a different host, list the proxy's exact IP in
`HSYNC_TRUSTED_PROXIES` (comma-separated; CIDR ranges are not expanded). The
server then reads `X-Forwarded-For` and uses the **rightmost hop that is not
itself a trusted proxy** — the address your proxy appended — so a client cannot
spoof its IP by sending a forged header prefix:

```dotenv
HSYNC_TRUSTED_PROXIES=10.0.0.5,10.0.0.6
```

Keep every proxy in the chain in the list. With a single proxy, prefer
overwriting the header (`proxy_set_header X-Forwarded-For $remote_addr`) as
defence in depth.

---

## TLS Configuration

### Always use HTTPS in production

The built-in SSL support (`HSYNC_SSL_ENABLED`) generates a persistent
self-signed certificate suitable for a **trusted private LAN**, but not for
exposing the server directly to the Internet. For an Internet-facing production
deployment:

- Use a reverse proxy (nginx, Caddy, Traefik) with Let's Encrypt
- Redirect all HTTP traffic to HTTPS
- Set HSTS headers in your reverse proxy config

Example nginx HSTS header:

```nginx
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
```

### Client-side certificate verification

- **Server certificates**: Always use a valid CA-signed certificate (Let's
  Encrypt is free and automated). Self-signed certs require `--ca-cert` or
  `--insecure` on every client, which is error-prone and insecure.
- **Client authentication**: HSYNC uses account-token Bearer tokens, not client
  certificates. The token is only sent over HTTPS — the client refuses to
  send it over plain HTTP.

---

## Filesystem Permissions

### Docker data directory

`./docker.sh` and the Compose template run the container under the invoking
user's UID/GID (`HSYNC_UID`/`HSYNC_GID`) and keep all persistent state in one
host data directory (`hsync-data/{buckets,state,certs}` by default). Because
the container runs as that same user, the bind-mounted directories are writable
without any manual `chown`. If you point a custom bind mount at directories
owned differently, make them writable by the configured user yourself.

### Restrict access to environment files

```bash
chmod 600 prod.env dev.env
```

These files contain the admin secret and other secrets. Only the user running the
server should be able to read them.

### Isolate bucket storage

Place the data directory on a dedicated partition. This prevents the server
from filling up the root filesystem and causing system-wide issues:

```bash
# Example: mount a dedicated volume at /data/hsync
sudo mkdir -p /data/hsync/buckets /data/hsync/state
export HSYNC_BUCKETS=/data/hsync/buckets
export HSYNC_STATE_DIR=/data/hsync/state
```

---

## Rate Limiting

Rate limits protect the server from abuse and accidental overload. Defaults are
reasonable for most deployments, but adjust them based on your workload:

| Variable | Default | When to increase |
|----------|---------|-----------------|
| `HSYNC_RATE_LIMIT_UPLOAD` | 200/min | Many clients uploading simultaneously |
| `HSYNC_RATE_LIMIT_DOWNLOAD` | 1000/min | High-traffic read-heavy workloads |
| `HSYNC_RATE_LIMIT_WS_MESSAGES` | 10/sec | High-frequency file change events |

> Setting any limit to `0` disables it entirely — not recommended for production.

---

## Request Body Limits

The server rejects an HTTP request body larger than `HSYNC_MAX_REQUEST_BODY_SIZE`
bytes **before** it parses or buffers it, and before authentication runs, so an
oversized body can never fill disk or memory first (HTTP `413`). By default the
cap is derived from the upload limit (`HSYNC_MAX_UPLOAD_SIZE` + 64 MiB
headroom); set it explicitly to a smaller value to tighten the surface, or to
`0` together with `HSYNC_MAX_UPLOAD_SIZE=0` to disable it entirely:

```bash
export HSYNC_MAX_REQUEST_BODY_SIZE=2147483648   # 2 GiB hard cap
```

A delta upload (`POST …/delta/apply`) rebuilds a file on disk, so it is bound by
the same `HSYNC_MAX_UPLOAD_SIZE` and `HSYNC_MIN_FREE_DISK_SPACE` guards as a full
upload — an oversized delta result is rejected instead of written.

---

## WebSocket Hardening

### Connection limits

Prevent a single misbehaving client from exhausting server resources:

```text
# Defaults are already conservative
HSYNC_WS_MAX_CONNECTIONS=1000          # global cap
HSYNC_WS_MAX_CONNECTIONS_PER_IP=10     # per-IP cap
```

### Heartbeat tuning

Shorter heartbeats detect dead connections faster but increase overhead:

```text
HSYNC_WS_HEARTBEAT_INTERVAL=30   # ping every 30s
HSYNC_WS_HEARTBEAT_TIMEOUT=10    # wait 10s for pong
```

A connection is dropped after `HEARTBEAT_INTERVAL + HEARTBEAT_TIMEOUT` (40s
by default) of silence. Increase these values for high-latency satellite links;
decrease them for LAN deployments where quick disconnect detection matters.

---

## Logging & Auditing

### Enable structured logging

JSON-formatted logs are easier to parse with log aggregation tools:

```dotenv
HSYNC_LOG_FORMAT=json
HSYNC_LOG_FILE=/project/logs/hsync-server.log
```

### What gets logged

- Every HTTP request (method, path, status code, duration, client IP)
- File uploads/downloads (bucket, filename, size)
- Authentication failures (with client IP)
- Rate limit violations (with client IP)
- WebSocket connections and disconnections

### What does NOT get logged

- Token/secret values (only the key prefix and account id/name are logged)
- File contents
- Request bodies

### Monitor auth failures

A spike in authentication failures (`401 Unauthorized`) may indicate a brute
force attempt. Set up an alert on your log aggregation platform for this
pattern.

---

## Checklist

- [ ] `HSYNC_ADMIN_KEY` is 64 hex chars, generated with `openssl rand -hex 32`
- [ ] `HSYNC_ADMIN_KEY` is stored in `prod.env` (chmod 600), never in version control
- [ ] Server port 9753 is **not** exposed to the internet
- [ ] Reverse proxy terminates TLS with a valid Let's Encrypt certificate
- [ ] HTTP → HTTPS redirect is configured
- [ ] Firewall only allows ports 22, 80, 443
- [ ] The host data directory is on a dedicated partition
- [ ] File permissions: `prod.env` is 600, the data directory is owned by the configured user
- [ ] Rate limits are configured (not set to 0)
- [ ] WebSocket connection limits are set
- [ ] Structured JSON logging is enabled for production
- [ ] Auth failure alerting is configured in monitoring

## See also

- [Deployment Guide](deployment.md) — Docker Compose, reverse proxy, troubleshooting.
- [Operations Guide](operations.md) — backup, upgrade, monitoring, performance.
- [Reverse Proxy Guide](reverse-proxy.md) — nginx and Traefik configuration.
- [SECURITY.md](../SECURITY.md) — supported versions, vulnerability reporting.
