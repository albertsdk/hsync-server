# Getting Started — Path B: Native (Poetry/systemd)

This path runs the HSYNC Server directly on the host as a Python service — for
a development machine, or for a managed host without Docker. Every command
below is host-native; on a Docker install follow
[Path A](getting-started-docker.md) instead.

> For a managed production host, complete the quick start here and then move to
> the [systemd deployment](deployment.md#systemd-service-without-docker).

## Prerequisites

- Python 3.12 or newer
- Poetry
- The `hsync-common` repository checked out as a sibling of `hsync-server`
  (a path dependency of the project)
- OpenSSL (to generate the bootstrap secret shown below)

## Install

From the `hsync-server` checkout:

```bash
poetry install --only main
```

## Start the server

Set the bootstrap admin secret **before the first start** (generate one with
`openssl rand -hex 32`), enable TLS so real clients can authenticate, and
start:

```bash
export HSYNC_ADMIN_KEY="$(openssl rand -hex 32)"
export HSYNC_SSL_ENABLED=true
export HSYNC_SSL_KEYFILE_PASSWORD="$(openssl rand -hex 32)"
export HSYNC_SSL_CERT_DIR=./certs
poetry run hsync-server
```

- The server listens on `https://localhost:9753` (default port).
- Bucket data is stored under `buckets/` and the causal-state database under
  `state/`, both under the project root (the settings default; the paths are
  resolved to absolute form at startup).
- On the **first start** the certificate is generated in `certs/`
  (`server.crt` / `server.key`); with `HSYNC_ADMIN_KEY` set, the bootstrap
  account is created **silently**.

Verify it is healthy in another terminal:

```bash
curl --insecure https://localhost:9753/health && echo "SERVER OK!"
```

> To reach clients on a private LAN you must bind a concrete address; the app
> listens on all interfaces by default, and the self-signed certificate is
> suitable for a trusted LAN only. For a public server follow the
> [Production Deployment Guide](deployment.md).

## Export the certificate for clients

Check the self-signed certificate fingerprint and copy the certificate to each
client over a trusted channel:

```bash
openssl x509 -in certs/server.crt -noout -fingerprint -sha256
```

Verify the displayed fingerprint out of band before trusting it. Do not use
`--insecure` for authenticated synchronization: disabling certificate
verification would expose the bearer token to a network attacker.

## Connect your clients

Every client authenticates with an account token (`hs_<id>_<secret>`) configured
as `HSYNC_API_KEY`. The admin CLI uses the same store as the server, so run
these commands with the server **stopped**, then restart it:

```bash
cd /path/to/hsync-server

# 1. Create an account for this client (the token is printed once)
poetry run hsync-server admin client add --name <client-name> --role client

# 2. Create the bucket the client will sync
poetry run hsync-server admin bucket create --bucket <bucket>

# 3. Give the client account access to the bucket
poetry run hsync-server admin bucket grant --bucket <bucket> --client <client-name>

# 4. Restart the server
poetry run hsync-server
```

> See [Roles and Permissions](roles-and-permissions.md#cli-tools) for the full
> command reference and how to invoke the CLI on other installation types.

Then configure the client on each device:

```bash
export HSYNC_API_KEY="hs_<id>_<secret>"
hsync server server.example.com -b photos -p /path/to/dir --ca-cert certs/server.crt
```

- `-s` — server URL;
- `-b` — the bucket created above;
- `-p` — local directory to sync;
- `--ca-cert` — the exported certificate from `certs/server.crt`.

> For all client options, see the
> [client Getting Started guide](https://github.com/albertobasso/hsync-client/blob/main/docs/getting-started.md).

## Use systemd for an always-on service

For a managed host, run the server as a systemd service instead of an
interactive terminal. The complete unit, hardening and day-2 steps are in
[Deployment → Systemd](deployment.md#systemd-service-without-docker). Note
that the unit pins `HSYNC_STATE_DIR=/opt/hsync/state` and
`HSYNC_BUCKETS=/opt/hsync/buckets` via `/opt/hsync/prod.env`: run the admin CLI
there with the same environment so it operates on the same store.

## Update

```bash
cd /path/to/hsync-server
git pull
poetry install --only main
```

Review the release notes and back up the data directory before updating. See
[Operations → Upgrade](operations.md#upgrade-guide) for details.

## Other installation methods

See [Installation Alternatives](installation-alternatives.md) for the manual
Compose setup, a temporary `docker run`, and the source Docker build with
`docker.sh`.
