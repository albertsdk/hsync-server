# Getting Started — Method 3: Debian/Ubuntu package

The `.deb` installs a frozen server under `/usr/lib/hsync-server` (with
`/usr/bin/hsync-server` on `PATH`), a hardened `hsync-server.service` systemd
unit, a dedicated `_hsync-server` system user, and a conffile at
`/etc/hsync-server/hsync-server.conf`. No Docker and no Python are required.

## Prerequisites

- <img src="assets/os/debian.svg" alt="Debian" width="16" height="16"> <img src="assets/os/ubuntu.svg" alt="Ubuntu" width="16" height="16"> Debian or Ubuntu, `amd64`, `arm64` or `armhf`
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

To build from the checkout, follow the build steps in
[Contributing](../CONTRIBUTING.md). `./compile.sh --target deb-all` cross-builds
the `amd64`, `arm64` and `armhf` packages inside Docker.

## Configure before the first start

The package never enables the service automatically. Edit the conffile as
root: set the rendezvous admin secret and enable TLS so real clients can
authenticate.

```bash
sudo sh -c 'sed -i \
  "s|^HSYNC_ADMIN_KEY=.*|HSYNC_ADMIN_KEY=$(openssl rand -hex 32)|" \
  /etc/hsync-server/hsync-server.conf'
sudo sed -i \
  's|^#HSYNC_SSL_ENABLED=false|HSYNC_SSL_ENABLED=true|' \
  /etc/hsync-server/hsync-server.conf
```

Data is stored under `/var/lib/hsync-server`; logs under `/var/log/hsync-server`.
The binary reads this conffile itself, exactly like the systemd unit, so
`sudo hsync-server` starts with the same settings as the service.

## Choose the listening port

The server listens on `HSYNC_PORT` (default `9753`) and every HSYNC client
connects over `https://` only — a plain-http server address is rejected.

- Default: leave `HSYNC_PORT` unset and enter the bare host name in the client
  (e.g. `rdv`); the client expands it to `https://rdv:9753`.
- Port 443: uncomment `HSYNC_PORT=443` in the conffile and enter `rdv:443` in
  the client. The unit grants `CAP_NET_BIND_SERVICE`, so the unprivileged
  `_hsync-server` user can bind it.
- Behind a reverse proxy: keep `HSYNC_SSL_ENABLED=false` and let the proxy
  terminate TLS on 443 — see [Reverse proxy](reverse-proxy.md).

Start the service with `systemctl`, not by running the binary as root: files a
root run creates in `/var/lib/hsync-server` (TLS identity, state DB) stay
root-owned and the `_hsync-server` service then cannot use them.

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

Run the admin CLI with the service **stopped** and restart it afterwards. The
binary reads `/etc/hsync-server/hsync-server.conf` itself, so no extra
environment setup is needed:

```bash
sudo systemctl stop hsync-server.service
sudo hsync-server admin tls fingerprint
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
sudo hsync-server admin client add --name CLIENT_NAME --role client
sudo hsync-server admin bucket create --bucket BUCKET
sudo hsync-server admin bucket grant --bucket BUCKET --client CLIENT_NAME
sudo systemctl start hsync-server.service
```

Then, on each device:

```bash
export HSYNC_API_KEY="hs_ACCOUNT_ID_ACCOUNT_SECRET"
hsync server server.example.com -b photos -p /path/to/dir \
  --ca-cert /trusted/path/server.crt
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
