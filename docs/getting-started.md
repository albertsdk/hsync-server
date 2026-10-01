# HSYNC Server — Getting Started

HSYNC Server is a file-sync hub: clients upload/download buckets, and changes
propagate to every connected client in real time.

Pick **one** installation method below, follow it to a running server, then
connect your clients. The methods are ordered from the simplest and most
immediate to the most involved.

| # | Method | Best for | Guide |
| - | ------ | -------- | ----- |
| 1 | **Installer script** (`install.sh` via `curl`) | Least setup on a host with Docker: one command, certificate and secrets handled | [Install with the script](getting-started-script.md) |
| 2 | **Docker / Compose** (manual) | You already manage containers, or want to build the image yourself | [Docker](getting-started-docker.md) |
| 3 | **Debian/Ubuntu package** (`.deb`) | A Debian/Ubuntu host without Docker, managed by systemd | [Debian package](getting-started-deb.md) |
| 4 | **From source** (clone and run) | Development, or a host where you control the toolchain | [Run from source](getting-started-source.md) |

All methods result in a server on `https://<server-ip>:9753` with a self-signed
certificate; only the toolchain differs. The installer script and the manual
Docker path use the same released container image.

> For a public Internet-facing server, complete a method first and then follow
> the [Production Deployment Guide](deployment.md).

## What's next after the server is running

- **Connect your clients** — every client authenticates with an account token
  (`hs_<id>_<secret>`) via its `HSYNC_API_KEY` variable. The method you chose
  shows the exact commands to create the account, create the bucket, and grant
  access.
- **Administrate** — accounts and buckets via the local CLI; the full command
  reference (and how to invoke it per installation type) is in
  [Roles and Permissions](roles-and-permissions.md).
- **Operate** — backup, restore, upgrade and monitoring in the
  [Operations Guide](operations.md).

> The client side of this flow is covered by the
> [client Getting Started](https://github.com/albertsdk/hsync-client/blob/main/docs/getting-started.md).
