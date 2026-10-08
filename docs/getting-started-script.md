# Getting Started — Method 1: Installer script

The `install.sh` installer is the recommended way to get a privately-hosted
server running: it downloads the released container image, generates the
required secrets, persists bucket data and certificates in a plain host
directory, and verifies that the server becomes healthy.

> This profile uses a persistent self-signed certificate: suitable for a
> trusted private LAN, not for exposing HSYNC directly to the Internet. For a
> public server, follow the [Production Deployment Guide](deployment.md).

## Prerequisites

- <img src="assets/os/linux.svg" alt="Linux" width="16" height="16"> <img src="assets/os/apple.svg" alt="macOS" width="16" height="16"> Linux or macOS with Docker Engine or Docker Desktop running
- Docker Compose v2 (`docker compose version`)
- OpenSSL
- `curl` and `sha256sum` for the verified download shown below

Docker must already be installed. The installer never invokes `sudo`, changes
firewall rules, or installs system packages.

## Install

### Quick install (one command, latest stable release)

```bash
curl -fsSL https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh | bash
```

This downloads and runs the installer in one step. The default installation
listens on `localhost` only — see [LAN setup](#lan-setup) below to bind a
concrete LAN address.

### Verified install (checksum)

To verify the download before running it:

```bash
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh"
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
chmod 700 install.sh
./install.sh
```

On macOS, use `shasum -a 256 install.sh` and compare the result with the
matching line in `SHA256SUMS`.

The default installation:

- stores configuration in `~/.local/share/hsync-server`;
- runs the latest stable `ghcr.io/albertsdk/hsync-server:vX.Y.Z` image;
- listens on port `9753` on localhost only;
- enables built-in HTTPS with a persistent self-signed certificate;
- stores files, causal state and certificates in the plain host directory
  `<install-dir>/hsync-data` (back it up; restore it to migrate);
- generates the bootstrap admin secret locally without printing it.

### LAN setup

To connect from other devices on a private LAN, provide the server's concrete
LAN address on the **first installation**. The installer also uses this address
as the container hostname so the generated certificate contains a matching IP
Subject Alternative Name:

```bash
curl -fsSL https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh \
  | bash -s -- --bind-address 192.168.1.20
```

The installer refuses to change the address later because the persisted
certificate identifies the original address. Moving the service to another IP
requires an explicit certificate rotation or a separate installation.

To install a **specific version** instead of the latest stable one, download the
installer from that release (replace `latest/download` with `download/vX.Y.Z`)
or pass the version explicitly: `./install.sh --version v1.0.0`. Conversely,
`--version latest` resolves and pins the latest stable release automatically.
Run `./install.sh --help` for version, port and install-directory options.

## Verify the server is healthy

The installer waits for the container health check. You can verify it again:

```bash
curl --insecure https://localhost:9753/health && echo "SERVER OK!"
```

## Export the certificate for clients

Read the bootstrap admin secret locally:

```bash
grep '^HSYNC_ADMIN_KEY=' "$HOME/.local/share/hsync-server/.env"
```

> On first start the server creates the bootstrap admin account with token
> `hs_HsyncAdminBootstrap01_<secret>` (silent when `HSYNC_ADMIN_KEY` is set).
> See [Roles and Permissions → Admin bootstrap](roles-and-permissions.md#admin-bootstrap).

Export the public certificate to each client:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
cd "$INSTALL_DIR"
docker compose exec server python -m hsync_server admin tls fingerprint
docker compose cp server:/project/certs/server.crt ./server.crt
```

The command prints the fingerprint as `sha256:<hex>`, the form clients pin with
`hsync trust server add`. Verify it out of band before trusting it. Rotate it
later with `docker compose exec server python -m hsync_server admin tls rotate`.

## Connect your clients

Create one account per client, create the bucket, and grant access:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
cd "$INSTALL_DIR"

docker compose exec server python -m hsync_server admin client add --name CLIENT_NAME --role client
docker compose exec server python -m hsync_server admin bucket create --bucket BUCKET
docker compose exec server python -m hsync_server admin bucket grant --bucket BUCKET --client CLIENT_NAME
```

Then, on each device, configure the client:

```bash
export HSYNC_API_KEY="hs_ACCOUNT_ID_ACCOUNT_SECRET"
hsync server server.example.com -b photos -p /path/to/dir \
  --ca-cert /trusted/path/server.crt
```

- `server.example.com` — the server hostname (positional argument; `https://` is implied);
- `-b` — the bucket created above;
- `-p` — local directory to sync;
- `--ca-cert` — the exported certificate.

> Full command reference: [Roles and Permissions](roles-and-permissions.md).
> Client options: [client Getting Started](https://github.com/albertsdk/hsync-client/blob/main/docs/getting-started.md).

## Manage and update

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
cd "$INSTALL_DIR"
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
docker compose up -d --wait
```

`docker compose down` does not delete the data directory. To update, download the
installer from the target release, verify it, and run it again with the same
installation directory: it updates the pinned image version while preserving
secrets and data.

```bash
./install.sh
```

Review the release notes and back up the buckets first. See
[Operations → Upgrade](operations.md#upgrade-guide) for details.

## Next

- Prefer to manage containers yourself? [Docker / Compose](getting-started-docker.md).
- No Docker host? [Debian package](getting-started-deb.md) or
  [run from source](getting-started-source.md).
