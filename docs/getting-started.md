# HSYNC Server — Getting Started

HSYNC Server is a file-sync hub: clients upload/download buckets, and changes
propagate to every connected client in real time.

Follow this guide in two steps:

1. **Choose your server** (Docker or native) — select one path below.
2. **Connect your clients** — create the account, the bucket, and grant access
   with the commands shown in the path you chose.

## Choose your path

| If you… | Follow |
| --- | --- |
| Have Docker (Linux or macOS) and want a ready-made setup with certificate management | [Path A — Docker](getting-started-docker.md) |
| Prefer a native Python service without containers (development machine, or a host you manage with systemd) | [Path B — Native (Poetry/systemd)](getting-started-native.md) |
| Prefer a packaged native install on Debian/Ubuntu (`dpkg -i`, no Poetry or Docker) | [Debian/Ubuntu package](installation-alternatives.md#debianubuntu-package) |

Both paths result in a running server on `https://<server-ip>:9753` with a
self-signed certificate; the difference is only the toolchain:

| | Path A — Docker | Path B — Native |
| --- | --- | --- |
| Install | `install.sh` installer (Compose) | Poetry + `hsync-common` sibling repo |
| Service | Docker containers + host data directory (bind mounts) | Process, optionally a systemd unit |
| Admin CLI | `docker compose exec server python -m hsync_server admin …` | `poetry run hsync-server admin …` |
| Upgrade | run the installer again | `git pull` + `poetry install` |
| Best for | private LAN, least setup | development, no-Docker / managed host |

> For a public Internet-facing server, complete a path first and then follow
> the [Production Deployment Guide](deployment.md).

## What's next after the server is running

- **Connect your clients** — every client authenticates with an account token
  (`hs_<id>_<secret>`) via its `HSYNC_API_KEY` variable. The path you chose
  shows the exact commands to create the account, create the bucket, and
  attribute it.
- **Administrate** — accounts and buckets via the local CLI; the full command
  reference (and how to invoke it per installation type) is in
  [Roles and Permissions](roles-and-permissions.md).
- **Operate** — backup, restore, upgrade and monitoring in the
  [Operations Guide](operations.md).

> The client side of this flow is covered by the
> [client Getting Started](https://github.com/albertsdk/hsync-client/blob/main/docs/getting-started.md).
