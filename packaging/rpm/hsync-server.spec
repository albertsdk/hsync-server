%global _hsync_pkgdir %{pkgdir}

Name:           hsync-server
Version:        __VERSION__
Release:        1%{?dist}
Summary:        HSYNC - file synchronization server

License:        BSD-3-Clause
URL:            https://github.com/albertsdk/hsync-server
Packager:       Alberto Basso <17047798+albertsdk@users.noreply.github.com>

# PyInstaller onedir bundle carries its own Python runtime and private shared
# libraries; rpm's automatic dependency scanner would mis-detect them. Declare
# the real runtime requirements explicitly.
AutoReqProv:    no
Requires:       glibc
Requires:       systemd
Requires(pre):  shadow-utils

%description
hsync-server is the central HTTP/WebSocket server of the HSYNC sync network.
It manages buckets (shared directories), member accounts and per-account
tokens, serves file upload/download with delta transfer, and pushes live
events to connected clients over WebSocket.

%prep
exit 0

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
cp -a %{_hsync_pkgdir}/. %{buildroot}/

%pre
getent group _hsync-server >/dev/null 2>&1 || groupadd -r _hsync-server
if ! id _hsync-server >/dev/null 2>&1; then
    useradd -r -g _hsync-server -s /sbin/nologin -d /nonexistent \
        -c "HSYNC file synchronization server" _hsync-server
fi
exit 0

%post
mkdir -p %{_sysconfdir}/hsync-server %{_localstatedir}/log/hsync-server \
    %{_localstatedir}/lib/hsync-server/buckets \
    %{_localstatedir}/lib/hsync-server/state \
    %{_localstatedir}/lib/hsync-server/certs
chown root:root %{_sysconfdir}/hsync-server 2>/dev/null || true
chmod 0750 %{_sysconfdir}/hsync-server
chown _hsync-server:_hsync-server %{_localstatedir}/log/hsync-server 2>/dev/null || true
chmod 0750 %{_localstatedir}/log/hsync-server
chown _hsync-server:_hsync-server %{_localstatedir}/lib/hsync-server 2>/dev/null || true
chmod 0750 %{_localstatedir}/lib/hsync-server
chown _hsync-server:_hsync-server %{_localstatedir}/lib/hsync-server/buckets \
    %{_localstatedir}/lib/hsync-server/state 2>/dev/null || true
chmod 0750 %{_localstatedir}/lib/hsync-server/buckets \
    %{_localstatedir}/lib/hsync-server/state
chown _hsync-server:_hsync-server %{_localstatedir}/lib/hsync-server/certs 2>/dev/null || true
chmod 0700 %{_localstatedir}/lib/hsync-server/certs
chown root:root %{_sysconfdir}/hsync-server/hsync-server.conf 2>/dev/null || true
chmod 0600 %{_sysconfdir}/hsync-server/hsync-server.conf 2>/dev/null || true
command -v restorecon >/dev/null 2>&1 && \
    restorecon -R %{_sysconfdir}/hsync-server \
    %{_localstatedir}/lib/hsync-server %{_localstatedir}/log/hsync-server \
    /usr/lib/systemd/system/hsync-server.service 2>/dev/null || true
# Register the unit but do NOT enable or start it: the administrator must set
# HSYNC_ADMIN_KEY and then opt in with ``systemctl enable --now hsync-server``.
systemctl daemon-reload 2>/dev/null || true
exit 0

%preun
if [ "$1" -eq 0 ]; then
    systemctl stop hsync-server.service 2>/dev/null || true
    systemctl disable hsync-server.service 2>/dev/null || true
fi
exit 0

%postun
# Data (buckets, state, certs, logs) and the system user are deliberately kept
# on uninstall: an RPM erase has no "purge" equivalent, and the Debian package
# only removes them on an explicit purge. Removing them here would destroy the
# buckets. Deleting the user/dirs is left to the administrator.
systemctl daemon-reload 2>/dev/null || true
exit 0

%files
%config(noreplace) %attr(0600,root,root) %{_sysconfdir}/hsync-server/hsync-server.conf
%dir %attr(0750,root,root) %{_sysconfdir}/hsync-server
%{_prefix}/lib/hsync-server
/usr/bin/hsync-server
%{_mandir}/man1/hsync-server.1.gz
/usr/lib/systemd/system/hsync-server.service

%changelog
