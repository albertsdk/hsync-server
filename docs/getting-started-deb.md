# Getting Started — Method 3: Debian/Ubuntu package

The `.deb` installs a frozen server under `/usr/lib/hsync-server` (with
`/usr/bin/hsync-server` on `PATH`), a hardened `hsync-server.service` systemd
unit, a dedicated `_hsync-server` system user, and a conffile at
`/etc/hsync-server/hsync-server.conf`. No Docker and no Python are required.

## Prerequisites

- Debian or Ubuntu, `amd64`, `arm64` or `armhf`
- A root shell or `sudo`

## Install

### From the release (recommended)

Download the latest package for your architecture and install it:

```bash
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/hsync-server-amd64.deb"
sudo dpkg -i hsync-server-amd64.deb
sudo apt install -f
```

The URL always points to the **latest stable** release; use `arm64` or `armhf`
instead of `amd64` on those architectures. To pin a version, use
`https://github.com/albertsdk/hsync-server/releases/download/vX.Y.Z/hsync-server_<version>_<arch>.deb`.

### Build from the checkout

From the `hsync-server` checkout (needs Poetry and the sibling `hsync-common`):

```bash
poetry install --with build
./compile.sh --target deb
# output: dist/deb/hsync-server_<ver>_<arch>.deb
```

`./compile.sh --target deb-all` cross-builds the `amd64`, `arm64` and `armhf`
packages inside Docker.

## Configure before the first start

The package never enables the service automatically. Edit the conffile as
root: set the bootstrap admin secret and enable TLS so real clients can
authenticate.

```bash
sudo sh -c 'sed -i "s|^HSYNC_ADMIN_KEY=.*|HSYNC_ADMIN_KEY=$(openssl rand -hex 32)|; s|^#HSYNC_SSL_ENABLED=false|HSYNC_SSL_ENABLED=true|" /etc/hsync-server/hsync-server.conf'
```

Data is stored under `/var/lib/hsync-server`; logs under `/var/log/hsync-server`.

## Start the service

```bash
sudo systemctl enable --now hsync-server.service
sudo systemctl status hsync-server.service
```

## Verify the server is healthy

```bash
curl --insecure https://localhost:9753/health && echo "SERVER OK!"
```

## Export the certificate for clients

The admin CLI uses the same conffile; run it with the service **stopped** and
restart it afterwards:

```bash
sudo systemctl stop hsync-server.service
sudo sh -c 'set -a; . /etc/hsync-server/hsync-server.conf; set +a; hsync-server admin tls fingerprint'
sudo cp /var/lib/hsync-server/certs/server.crt ./server.crt
sudo systemctl start hsync-server.service
```

The command prints the fingerprint as `sha256:<hex>`, the form clients pin with
`hsync trust server add`. Verify it out of band before trusting it.

## Connect your clients

Create one account per client, create the bucket, and grant access (service
stopped, then restart):

```bash
sudo systemctl stop hsync-server.service
sudo sh -c 'set -a; . /etc/hsync-server/hsync-server.conf; set +a; hsync-server admin client add --name CLIENT_NAME --role client'
sudo sh -c 'set -a; . /etc/hsync-server/hsync-server.conf; set +a; hsync-server admin bucket create --bucket BUCKET'
sudo sh -c 'set -a; . /etc/hsync-server/hsync-server.conf; set +a; hsync-server admin bucket grant --bucket BUCKET --client CLIENT_NAME'
sudo systemctl start hsync-server.service
```

Then, on each device:

```bash
export HSYNC_API_KEY="hs_<id>_<secret>"
hsync server server.example.com -b photos -p /path/to/dir --ca-cert /trusted/path/server.crt
```

> Full command reference: [Roles and Permissions](roles-and-permissions.md).

## Manage and update

```bash
sudo systemctl status hsync-server.service
sudo journalctl -u hsync-server.service -f
sudo systemctl restart hsync-server.service
```

To update, install the newer `.deb` over the existing one (`sudo dpkg -i …`);
the conffile and data are preserved. Review the release notes and back up
`/var/lib/hsync-server` first. See
[Operations → Upgrade](operations.md#upgrade-guide).

## Next

- Prefer containers? [Installer script](getting-started-script.md) or
  [Docker](getting-started-docker.md).
- Running directly from a clone? [From source](getting-started-source.md).
