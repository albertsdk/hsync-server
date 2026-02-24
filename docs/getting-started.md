# HSYNC Server — Getting Started

This is the single recommended quick start. It installs a
released multi-architecture container with Docker Compose, generates the required
secrets, persists bucket data and certificates in Docker volumes, and verifies
that the server becomes healthy.

> This profile uses a persistent self-signed certificate. It is suitable for a
> trusted private LAN, but not for exposing HSYNC directly to the Internet. For a
> public server, follow the [Production Deployment Guide](deployment.md).

## Prerequisites

- Linux or macOS with Docker Engine or Docker Desktop running
- Docker Compose v2 (`docker compose version`)
- OpenSSL
- `curl` and `sha256sum` for the verified download shown below

Docker must already be installed. The HSYNC installer never invokes `sudo`,
changes firewall rules, or installs system packages.

## Install

### Quick install (one command, latest stable release)

```bash
curl -fsSL https://github.com/albertobasso/hsync-server/releases/latest/download/install.sh | bash
```

This downloads and runs the installer in one step. The default installation
listens on `localhost` only — see [LAN setup](#lan-setup) below to bind a
concrete LAN address.

### Verified install (checksum)

If you prefer to verify the download before running it:

```bash
curl -fLO "https://github.com/albertobasso/hsync-server/releases/latest/download/install.sh"
curl -fLO "https://github.com/albertobasso/hsync-server/releases/latest/download/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
chmod 700 install.sh
./install.sh
```

On macOS, use `shasum -a 256 install.sh` and compare the result
with the matching line in `SHA256SUMS`.

The default installation:

- stores configuration in `~/.local/share/hsync-server`;
- runs the latest stable `ghcr.io/albertobasso/hsync-server:vX.Y.Z` image;
- listens on port `9753` on localhost only;
- enables built-in HTTPS with a persistent self-signed certificate;
- stores files and certificates in Docker named volumes;
- generates the API key and private-key password locally without printing them.

### LAN setup

To connect from other devices on a private LAN, provide the server's concrete
LAN address on the **first installation**. The installer also uses this address
as the container hostname so the generated certificate contains a matching IP
Subject Alternative Name:

```bash
# Quick install with a LAN bind address
curl -fsSL https://github.com/albertobasso/hsync-server/releases/latest/download/install.sh | bash -s -- --bind-address 192.168.1.20

# Or, with the verified download:
./install.sh --bind-address 192.168.1.20
```

The installer refuses to change the address later because the persisted
certificate identifies the original address. Moving the service to another IP
requires an explicit certificate rotation or a separate installation, followed
by re-establishing client trust.

Run `./install.sh --help` for version, port, install-directory,
validation-only, and dry-run options.

To install a **specific version** instead of the latest stable one, download the
installer from that release (replace `latest/download` with `download/vX.Y.Z`
in the URLs above) or pass the version explicitly:
`./install.sh --version v1.0.0`. Conversely, `--version latest` makes any
installer resolve and pin the latest stable release automatically.

## Verify and connect

The installer waits for the container health check. You can verify it again with:

```bash
curl --insecure https://localhost:9753/health
```

Read the generated admin token locally:

```bash
grep '^HSYNC_ADMIN_KEY=' "$HOME/.local/share/hsync-server/.env"
```

> The installer generates a 64-hex `HSYNC_ADMIN_KEY`. The server uses it as the
> bootstrap admin secret and prints the full account token
> (`hs_HsyncAdminBootstrap01_<secret>`) once at first start. See
> [Roles and Permissions](roles-and-permissions.md).

Export the public certificate and copy it to each client over a trusted channel:

```bash
cd "$HOME/.local/share/hsync-server"
docker compose cp server:/project/certs/server.crt ./server.crt
openssl x509 -in server.crt -noout -fingerprint -sha256
```

Configure clients with:

- server URL: `https://<server-ip>:9753`;
- the generated admin account token value;
- `--ca-cert /trusted/path/server.crt` to verify the self-signed certificate.

Verify the displayed certificate fingerprint out of band before trusting it.
Do not use `--insecure` for authenticated synchronization: disabling certificate
verification would expose the bearer token to a network attacker.

See the client [Getting Started guide](https://github.com/albertobasso/hsync-client/blob/main/docs/getting-started.md)
for client-side configuration.

## Manage the server

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
cd "$INSTALL_DIR"
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
docker compose up -d --wait
```

`docker compose down` does not delete the named data volumes. Do not add `-v`
unless you intentionally want to delete all server files and persisted
certificates.

## Update

Download the installer from the target release, verify it, and run it again with
the same installation directory. It updates the pinned image version while
preserving secrets and volumes:

```bash
./install.sh
```

Review the release notes and back up the buckets before updating. See the
[Operations Guide](operations.md) for backup and restore procedures.

## Other installation methods

The bootstrap above is the supported quick start, not the only deployment
method. See [Installation Alternatives](installation-alternatives.md) for a
manual Compose setup, a temporary `docker run`, a source build, and a native
Poetry/systemd installation.
