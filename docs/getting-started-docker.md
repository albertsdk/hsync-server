# Getting Started — Path A: Docker

This path installs a released multi-architecture container with Docker
Compose, generates the required secrets, persists bucket data and certificates
in a plain host data directory, and verifies that the server becomes healthy.
Every command below is Docker-specific; on a native install follow
[Path B](getting-started-native.md) instead.

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
curl -fsSL https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh | bash
```

This downloads and runs the installer in one step. The default installation
listens on `localhost` only — see [LAN setup](#lan-setup) below to bind a
concrete LAN address.

### Verified install (checksum)

If you prefer to verify the download before running it:

```bash
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh"
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
chmod 700 install.sh
./install.sh
```

On macOS, use `shasum -a 256 install.sh` and compare the result
with the matching line in `SHA256SUMS`.

The default installation:

- stores configuration in `~/.local/share/hsync-server`;
- runs the latest stable `ghcr.io/albertsdk/hsync-server:vX.Y.Z` image;
- listens on port `9753` on localhost only;
- enables built-in HTTPS with a persistent self-signed certificate;
- stores files, causal state and certificates in the plain host directory
  `<install-dir>/hsync-data` (back it up; restore it to migrate);
- generates the bootstrap admin secret and private-key password locally without
  printing them.

### LAN setup

To connect from other devices on a private LAN, provide the server's concrete
LAN address on the **first installation**. The installer also uses this address
as the container hostname so the generated certificate contains a matching IP
Subject Alternative Name:

```bash
# Quick install with a LAN bind address
curl -fsSL https://github.com/albertsdk/hsync-server/releases/latest/download/install.sh | bash -s -- --bind-address 192.168.1.20

# Or, with the verified download:
./install.sh --bind-address 192.168.1.20
```

The installer refuses to change the address later because the persisted
certificate identifies the original address. Moving the service to another IP
requires an explicit certificate rotation or a separate installation, followed
by re-establishing client trust.

Run `./install.sh --help` for version, port, install-directory,
validation-only, and dry-run options.

To install a **specific version** instead of the latest stable one, download
the installer from that release (replace `latest/download` with
`download/vX.Y.Z` in the URLs above) or pass the version explicitly:
`./install.sh --version v1.0.0`. Conversely, `--version latest` makes any
installer resolve and pin the latest stable release automatically.

## Verify the server is healthy

The installer waits for the container health check. You can verify it again
with:

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

Export the public certificate and copy it to each client over a trusted
channel:

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"
cd "$INSTALL_DIR"
docker compose cp server:/project/certs/server.crt ./server.crt
openssl x509 -in server.crt -noout -fingerprint -sha256
```

Verify the displayed fingerprint out of band before trusting it. Do not use
`--insecure` for authenticated synchronization: disabling certificate
verification would expose the bearer token to a network attacker.

## Connect your clients

Every client authenticates with an account token (`hs_<id>_<secret>`) configured
as `HSYNC_API_KEY`. With the server running, create a dedicated account per
client, create the bucket, and attribute it (run these once per client/bucket;
full syntax in [Roles and Permissions](roles-and-permissions.md)):

```bash
INSTALL_DIR="$HOME/.local/share/hsync-server"

# 1. Create an account for this client (the token is printed once)
cd "$INSTALL_DIR" && docker compose exec server \
  python -m hsync_server admin client add \
  --name CLIENT_NAME --role client

# 2. Create the bucket the client will sync
cd "$INSTALL_DIR" && docker compose exec server \
  python -m hsync_server admin bucket create --bucket BUCKET

# 3. Give the client account access to the bucket
cd "$INSTALL_DIR" && docker compose exec server \
  python -m hsync_server admin bucket grant \
  --bucket BUCKET --client CLIENT_NAME
```

> The bootstrap admin token (`hs_HsyncAdminBootstrap01_<secret>`) from step
> **Export the certificate for clients** has full access and needs none of
> these steps for evaluation. See [Roles and Permissions](roles-and-permissions.md#cli-tools)
> for the full command reference.

Then configure the client on each device:

```bash
export HSYNC_API_KEY="hs_<id>_<secret>"
hsync server server.example.com -b photos -p /path/to/dir --ca-cert /trusted/path/server.crt
```

- `server.example.com` — the server hostname (positional argument; `https://` is implied);
- `-b` — the bucket created above;
- `-p` — local directory to sync;
- `--ca-cert` — the exported certificate.

> For all client options, see the
> [client Getting Started guide](https://github.com/albertsdk/hsync-client/blob/main/docs/getting-started.md).

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

`docker compose down` does not delete the data directory. Remove
`<install-dir>/hsync-data` only if you intentionally want to delete all files
and persisted certificates.

## Update

Download the installer from the target release, verify it, and run it again
with the same installation directory. It updates the pinned image version
while preserving secrets and data:

```bash
./install.sh
```

Review the release notes and back up the buckets before updating. See
[Operations → Upgrade](operations.md#upgrade-guide) for details.

## Other installation methods

This bootstrap is the supported Docker quick start. For manual Compose, a
temporary `docker run`, and the source Docker build, see
[Installation Alternatives](installation-alternatives.md).
