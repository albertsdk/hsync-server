# HSYNC Server — Security Best Practices

This page covers hardening recommendations for production HSYNC Server
deployments. Follow these guidelines to minimize the attack surface and protect
your data.

> The [SECURITY.md](../SECURITY.md) file covers supported versions and how to
> report vulnerabilities.

## Table of Contents

- [API Key Management](#api-key-management)
- [Network Exposure](#network-exposure)
- [TLS Configuration](#tls-configuration)
- [Filesystem Permissions](#filesystem-permissions)
- [Rate Limiting](#rate-limiting)
- [WebSocket Hardening](#websocket-hardening)
- [Logging & Auditing](#logging--auditing)
- [Checklist](#checklist)

---

## API Key Management

### Generate a strong key

Use a cryptographically random key of at least 32 bytes:

```bash
openssl rand -base64 32
# Output: aGk7Xp2Qm9vR3sL1wN8yB4tF6dH0jK5cA...
```

### Rotate the key

Rotate the API key periodically (e.g., every 90 days) and immediately if a key
is suspected compromised:

1. Generate a new key
2. Update `prod.env` with the new key
3. Restart the server: `./docker.sh restart`
4. Distribute the new key to all clients

> There is no built-in mechanism for dual-key rotation windows. During rotation,
> existing clients will briefly fail to connect until they receive the new key.

### Never expose the key

- Store the API key **only** in `prod.env`, never in shell history or version
  control
- Add `prod.env` and `dev.env` to `.gitignore` (already done in the template)
- Use environment variable injection from a secrets manager (Vault, SOPS, etc.)
  in automated deployments

---

## Network Exposure

### Only expose ports 80 and 443

The HSYNC Server itself listens on port `8765` by default. **Never expose this
port directly to the internet.** Instead:

```
Internet ──► Reverse Proxy (:443) ──► HSYNC Server (:8765, localhost only)
```

In Docker Compose, use `expose` (not `ports`) for the hsync-server service:

```yaml
hsync-server:
  expose:
    - "8765"           # internal only
  # Do NOT add:
  # ports:
  #   - "8765:8765"    # this exposes the port publicly
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

> Port `8765` should **not** be opened. Only the reverse proxy (nginx, Caddy,
> Traefik) on ports 80/443 needs external access.

### Trusted proxies

If your reverse proxy is on a different host, configure `HSYNC_TRUSTED_PROXIES`
with the proxy's IP address or subnet so the server correctly resolves client
IPs from `X-Forwarded-For` headers:

```bash
HSYNC_TRUSTED_PROXIES=10.0.0.5,172.16.0.0/12
```

---

## TLS Configuration

### Always use HTTPS in production

The built-in SSL support (`HSYNC_SSL_ENABLED`) generates self-signed
certificates suitable **only** for development and LAN testing. For production:

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
- **Client authentication**: HSYNC uses API key Bearer tokens, not client
  certificates. The API key is only sent over HTTPS — the client refuses to
  send it over plain HTTP.

---

## Filesystem Permissions

### Docker volumes

The container runs as UID 1000. Ensure the host directories used for bind mounts
are owned by the same UID:

```bash
sudo chown -R 1000:1000 ./buckets ./certs ./logs
```

### Restrict access to environment files

```bash
chmod 600 prod.env dev.env
```

These files contain the API key and other secrets. Only the user running the
server should be able to read them.

### Isolate bucket storage

Place bucket data on a dedicated volume or partition. This prevents the server
from filling up the root filesystem and causing system-wide issues:

```bash
# Example: mount a dedicated volume at /data/hsync
sudo mkdir -p /data/hsync/buckets
export HSYNC_BUCKETS=/data/hsync/buckets
```

---

## Rate Limiting

Rate limits protect the server from abuse and accidental overload. Defaults are
reasonable for most deployments, but adjust them based on your workload:

| Variable | Default | When to increase |
|----------|---------|-----------------|
| `HSYNC_RATE_LIMIT_UPLOAD` | 200/min | Many clients uploading simultaneously |
| `HSYNC_RATE_LIMIT_DOWNLOAD` | 1000/min | High-traffic read-heavy workloads |
| `HSYNC_RATE_LIMIT_WEBSOCKET` | 50/min | Many clients reconnecting after network blips |
| `HSYNC_RATE_LIMIT_WS_MESSAGES` | 10/sec | High-frequency file change events |

> Setting any limit to `0` disables it entirely — not recommended for production.

---

## WebSocket Hardening

### Connection limits

Prevent a single misbehaving client from exhausting server resources:

```bash
# Defaults are already conservative
HSYNC_WS_MAX_CONNECTIONS=1000          # global cap
HSYNC_WS_MAX_CONNECTIONS_PER_IP=10     # per-IP cap
```

### Heartbeat tuning

Shorter heartbeats detect dead connections faster but increase overhead:

```bash
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

```bash
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

- API key values (only the presence/absence is checked)
- File contents
- Request bodies

### Monitor auth failures

A spike in authentication failures (`401 Unauthorized`) may indicate a brute
force attempt. Set up an alert on your log aggregation platform for this
pattern.

---

## Checklist

- [ ] API key is at least 32 bytes, generated with `openssl rand -base64 32`
- [ ] API key is stored in `prod.env` (chmod 600), never in version control
- [ ] Server port 8765 is **not** exposed to the internet
- [ ] Reverse proxy terminates TLS with a valid Let's Encrypt certificate
- [ ] HTTP → HTTPS redirect is configured
- [ ] Firewall only allows ports 22, 80, 443
- [ ] Bucket volumes are on a dedicated partition
- [ ] File permissions: `prod.env` is 600, buckets are owned by UID 1000
- [ ] Rate limits are configured (not set to 0)
- [ ] WebSocket connection limits are set
- [ ] Structured JSON logging is enabled for production
- [ ] Auth failure alerting is configured in monitoring

## See also

- [Deployment Guide](deployment.md) — Docker Compose, reverse proxy, troubleshooting.
- [Operations Guide](operations.md) — backup, upgrade, monitoring, performance.
- [Reverse Proxy Guide](reverse-proxy.md) — nginx and Traefik configuration.
- [SECURITY.md](../SECURITY.md) — supported versions, vulnerability reporting.
