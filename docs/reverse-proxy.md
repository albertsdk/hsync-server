# HSYNC Server — Reverse Proxy Configuration

The HSYNC Server does **not** include a built-in TLS terminator for production.
Always run it behind a reverse proxy that terminates HTTPS. This page covers
Nginx and Traefik.

> Caddy is the simplest option and is covered in the
> [Docker Compose section of the Deployment Guide](deployment.md#docker-compose-recommended-for-production).

## Table of Contents

- [Nginx](#nginx)
- [Traefik](#traefik)

---

## Nginx

```nginx
# /etc/nginx/sites-available/hsync
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    client_max_body_size 10g;

    location / {
        proxy_pass http://127.0.0.1:9753;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
    }
}

server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

> **Note**: The `proxy_read_timeout 86400s` (24 hours) is important for long-lived
> WebSocket connections. Without it, nginx may close idle connections prematurely.

## Traefik

```yaml
# docker-compose.yml with Traefik
services:
  traefik:
    image: traefik:v3.0
    container_name: traefik
    restart: unless-stopped
    command:
      - "--providers.docker=true"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=admin@your-domain.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "443:443"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock:ro"
      - "traefik-le:/letsencrypt"
    networks:
      - hsync-net

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
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.hsync.rule=Host(`your-domain.com`)"
      - "traefik.http.routers.hsync.entrypoints=websecure"
      - "traefik.http.routers.hsync.tls.certresolver=letsencrypt"
      - "traefik.http.services.hsync.loadbalancer.server.port=9753"
    networks:
      - hsync-net

volumes:
  traefik-le:

networks:
  hsync-net:
    driver: bridge
```

## See also

- [Deployment Guide](deployment.md) — Docker Compose with Caddy, systemd service, and troubleshooting.
- [Operations Guide](operations.md) — backup, upgrade, monitoring, performance.
