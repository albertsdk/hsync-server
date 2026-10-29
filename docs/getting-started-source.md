# Getting Started — Method 4: From source

Run the server directly from a clone as a Python service — for development, or a
host where you control the toolchain. This is the most involved method; for a
managed host prefer the [installer script](getting-started-script.md) or the
[Debian package](getting-started-deb.md).

> For a managed production host, complete the quick start here and then move to
> the [systemd deployment](deployment.md#systemd-service-without-docker).

## Prerequisites

- Python 3.12 or newer
- Poetry
- The `hsync-common` repository checked out as a sibling of `hsync-server`
  (a path dependency of the project)
- OpenSSL (to generate the rendezvous secret shown below)

## Install and run

### Quick path (`run.sh`)

With both repositories cloned side by side:

```bash
git clone https://github.com/albertsdk/hsync-server.git hsync-server
git clone https://github.com/albertsdk/hsync-common.git hsync-common
cd hsync-server
./run.sh
```

`run.sh` creates the in-project virtualenv on first use, installs the main
dependencies, and starts the server, forwarding any argument to `hsync-server`
(`./run.sh --help`). Its prerequisites are enforced: it stops with a clear
message if `../hsync-common` is missing.

### Native commands

The same steps, spelled out (the virtualenv is created by `run.sh`):

```bash
export HSYNC_ADMIN_KEY="$(openssl rand -hex 32)"
.venv/bin/hsync-server --ssl
```

- The server listens on `https://localhost:9753` (default port).
- Bucket data is stored under `buckets/` and the causal-state database under
  `state/`, both under the project root.
- On the **first start** the certificate is generated in `certs/`; with
  `HSYNC_ADMIN_KEY` set, the rendezvous account is created silently.

Set the secrets **before the first start**. Verify it is healthy in another
terminal:

```bash
curl --insecure https://localhost:9753/health && echo "SERVER OK!"
```

> To reach clients on a private LAN bind a concrete address; the app listens on
> all interfaces by default, and the self-signed certificate is suitable for a
> trusted LAN only. For a public server follow the
> [Production Deployment Guide](deployment.md).

## Standalone binary (no Python on the target)

`compile.sh` freezes the server into a self-contained executable (install the
build dependencies first — see [Contributing](../CONTRIBUTING.md)):

```bash
./compile.sh
# → dist/<os>-<arch>/hsync-server
./dist/linux-amd64/hsync-server --version
```

Raspberry Pi OS executables (`armhf`/`arm64`) are cross-built inside Docker:

```bash
./compile.sh --target rpi
# → dist/rpi/armhf/hsync-server and dist/rpi/arm64/hsync-server
```

The executable reads the same `HSYNC_*` variables and CLI options as the native
install (`./compile.sh --help` lists them). See the
[Debian package](getting-started-deb.md) if you want a managed install instead.

## Export the certificate for clients

```bash
hsync-server admin tls fingerprint
```

The command prints the fingerprint as `sha256:<hex>`, the form clients pin with
`hsync trust server add`. Verify it out of band before trusting it. Rotate it
later with `hsync-server admin tls rotate`.

## Connect your clients

Every client authenticates with an account token (`hs_<id>_<secret>`) in
`HSYNC_API_KEY`. The admin CLI uses the same store as the server, so run these
commands with the server **stopped**, then restart it:

```bash
cd /path/to/hsync-server

# 1. Create an account for this client (the token is printed once)
.venv/bin/hsync-server admin client add --name CLIENT_NAME --role client

# 2. Create the bucket the client will sync
.venv/bin/hsync-server admin bucket create --bucket BUCKET

# 3. Give the client account access to the bucket
.venv/bin/hsync-server admin bucket grant --bucket BUCKET --client CLIENT_NAME

# 4. Restart the server
.venv/bin/hsync-server
```

Then, on each device:

```bash
export HSYNC_API_KEY="hs_ACCOUNT_ID_ACCOUNT_SECRET"
hsync server server.example.com -b photos -p /path/to/dir \
  --ca-cert certs/server.crt
```

> Full command reference: [Roles and Permissions](roles-and-permissions.md).

## Use systemd for an always-on service

For a managed host, run the server as a systemd service instead of an
interactive terminal. The complete unit, hardening and day-2 steps are in
[Deployment → Systemd](deployment.md#systemd-service-without-docker).

## Update

```bash
cd /path/to/hsync-server
git pull
# re-run ./run.sh if the dependencies changed
```

Review the release notes and back up the data directory before updating. See
[Operations → Upgrade](operations.md#upgrade-guide) for details.

## Next

- Prefer a packaged install? [Debian package](getting-started-deb.md).
- Prefer containers? [Installer script](getting-started-script.md) or
  [Docker](getting-started-docker.md).
