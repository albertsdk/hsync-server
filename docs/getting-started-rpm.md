# Getting Started — RPM package (RHEL/Rocky/Alma/Fedora)

The `.rpm` installs a frozen server under `/usr/lib/hsync-server` (with
`/usr/bin/hsync-server` on `PATH`), the hardened `hsync-server.service` systemd
unit, a dedicated `_hsync-server` system user, and a conffile at
`/etc/hsync-server/hsync-server.conf`. No Docker and no Python are required.

> The RPM binaries are built on a Rocky Linux 9 baseline (glibc 2.34), so they
> run on RHEL/Rocky/Alma 9+, recent Fedora, and any distro with glibc ≥ 2.34.
> Debian/Ubuntu is covered by the [Debian package](getting-started-deb.md).

## Prerequisites

- RHEL, Rocky Linux, AlmaLinux (9+) or Fedora, on `x86_64` or `aarch64`
- A root shell or `sudo`

## Install

```bash
curl -fLO "https://github.com/albertsdk/hsync-server/releases/latest/download/hsync-server-x86_64.rpm"
sudo dnf install hsync-server-x86_64.rpm
```

The URL always points to the **latest stable** release; use `aarch64` instead of
`x86_64` on ARM. To pin a version, use
`https://github.com/albertsdk/hsync-server/releases/download/vX.Y.Z/hsync-server-<version>-1.el9.<arch>.rpm`.

To build from the checkout instead, follow the build steps in
[Contributing](../CONTRIBUTING.md).

## Configure and run

The package behaves exactly like the Debian package: it never enables the
service automatically. Follow the steps in
[Debian package → Configure before the first start](getting-started-deb.md#configure-before-the-first-start)
through
[→ Connect your clients](getting-started-deb.md#connect-your-clients), using
`sudo dnf upgrade hsync-server-*.rpm` instead of `dpkg -i` to update.

Data is stored under `/var/lib/hsync-server`; logs under
`/var/log/hsync-server`.

## Next

- Prefer containers? [Installer script](getting-started-script.md) or
  [Docker](getting-started-docker.md).
- Full command reference: [Roles and Permissions](roles-and-permissions.md).
