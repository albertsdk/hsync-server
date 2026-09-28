# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
SSL/TLS utilities for HSYNC Server.

This module provides utilities for generating and managing self-signed
certificates for HTTPS/WSS communication. Certificates are automatically
generated if not present, enabling encrypted communication out of the box.

The identity follows the shared service-identity model (ADR-0037): ECDSA P-256,
an unencrypted PKCS#8 private key guarded by ``0600``/``0700`` filesystem
permissions (SSH host-key model, not a passphrase), and a 20-year validity. The
certificate is validated by pinning its fingerprint (ADR-0036), not by WebPKI.

Usage:
    from hsync_server.ssl_utils import ensure_certificates, get_ssl_config

    # Ensure certificates exist (generates if missing)
    cert_file, key_file = ensure_certificates(HSYNC_SSL_CERT_DIR)

    # Get SSL configuration for uvicorn
    ssl_config = get_ssl_config(ssl_enabled=True, ssl_cert_dir=HSYNC_SSL_CERT_DIR)
"""

import ipaddress
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID
from hsync_common.ssl_utils import (
    SERVICE_CERT_VALIDITY_DAYS,
    build_self_signed_certificate,
    generate_ecdsa_private_key,
    serialize_cert_pem,
    serialize_private_key_unencrypted,
)
from loguru import logger as log

# Default certificate validity: the shared service-identity lifetime (20 years).
# The certificate is authenticated by fingerprint pinning (ADR-0036/ADR-0037),
# not by WebPKI, so it lives for the deployment instead of a browser-friendly
# 398/825-day window.
DEFAULT_CERT_VALIDITY_DAYS = SERVICE_CERT_VALIDITY_DAYS

# Identity file permissions: key 0600, certificate 0644, parent directory 0700.
_KEY_FILE_MODE = 0o600
_CERT_FILE_MODE = 0o644
_DIR_MODE = 0o700


def generate_self_signed_cert(
    cert_path: str,
    key_path: str,
    hostname: str | None = None,
    validity_days: int = DEFAULT_CERT_VALIDITY_DAYS,
) -> tuple[str, str]:
    """
    Generate a self-signed certificate and its private key.

    The key is ECDSA P-256 and is written unencrypted (PKCS#8) with ``0600``
    permissions in a ``0700`` directory — the SSH host-key / Syncthing model
    (ADR-0037): the identity is trusted by fingerprint pinning, so the secret is
    protected by filesystem permissions rather than a passphrase.

    Args:
        cert_path: Path where the certificate will be saved.
        key_path: Path where the private key will be saved.
        hostname: Hostname for the certificate (defaults to system hostname).
        validity_days: Certificate validity period in days.

    Returns:
        Tuple of (cert_path, key_path).

    Raises:
        OSError: If unable to write certificate files.
    """
    if hostname is None:
        hostname = socket.gethostname()

    log.info(f"Generating self-signed certificate for hostname: {hostname}")

    private_key = generate_ecdsa_private_key()

    subject_attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IT"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Italy"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Self-Signed"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ]

    # Build certificate with Subject Alternative Names (SAN)
    # Include both hostname and localhost for local development
    san_list = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]

    # Try to add local IP addresses
    try:
        local_ip = socket.gethostbyname(hostname)
        san_list.append(x509.IPAddress(ipaddress.IPv4Address(local_ip)))
    except (socket.gaierror, ValueError):
        pass  # Hostname doesn't resolve, skip

    cert = build_self_signed_certificate(
        private_key,
        subject_attrs=subject_attrs,
        san_list=san_list,
        validity_days=validity_days,
    )

    not_valid_after = datetime.now(UTC) + timedelta(days=validity_days)

    # Ensure parent directories exist; when newly created, restrict to 0700 (the
    # private key lives here).
    for parent in (Path(cert_path).parent, Path(key_path).parent):
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, _DIR_MODE)

    # Write the private key unencrypted (PKCS#8), protected by permissions.
    with open(key_path, "wb") as f:
        f.write(serialize_private_key_unencrypted(private_key))
    os.chmod(key_path, _KEY_FILE_MODE)  # Read/write only for owner

    # Write certificate
    with open(cert_path, "wb") as f:
        f.write(serialize_cert_pem(cert))
    os.chmod(cert_path, _CERT_FILE_MODE)  # Readable by all

    log.info(f"Certificate generated: {cert_path}")
    log.info(f"Private key generated: {key_path}")
    log.info(f"Certificate valid until: {not_valid_after.isoformat()}")

    return cert_path, key_path


def ensure_certificates(
    cert_dir: str,
    cert_filename: str = "server.crt",
    key_filename: str = "server.key",
    hostname: str | None = None,
) -> tuple[str, str]:
    """
    Ensure SSL certificates exist, generating them if necessary.

    Args:
        cert_dir: Directory where certificates are stored.
        cert_filename: Certificate filename.
        key_filename: Private key filename.
        hostname: Hostname for the certificate.

    Returns:
        Tuple of (cert_path, key_path).
    """
    cert_path = str(Path(cert_dir) / cert_filename)
    key_path = str(Path(cert_dir) / key_filename)

    if Path(cert_path).exists() and Path(key_path).exists():
        log.info(f"Using existing certificates from: {cert_dir}")
        return cert_path, key_path

    log.info("Certificates not found, generating new self-signed certificates...")
    return generate_self_signed_cert(cert_path, key_path, hostname=hostname)


def get_ssl_config(
    ssl_enabled: bool,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    ssl_cert_dir: str | None = None,
) -> dict[str, Any]:
    """
    Get SSL configuration for uvicorn.

    Args:
        ssl_enabled: Whether SSL is enabled.
        ssl_certfile: Path to SSL certificate file.
        ssl_keyfile: Path to SSL private key file.
        ssl_cert_dir: Directory for auto-generated certificates.

    Returns:
        Dictionary with ``ssl_certfile`` and ``ssl_keyfile`` keys (or empty if
        SSL is disabled). The private key is stored unencrypted (ADR-0037), so
        no ``ssl_keyfile_password`` is required.
    """
    if not ssl_enabled:
        return {}

    # If explicit paths provided, use them
    if ssl_certfile and ssl_keyfile:
        if not Path(ssl_certfile).exists():
            raise FileNotFoundError(f"SSL certificate not found: {ssl_certfile}")
        if not Path(ssl_keyfile).exists():
            raise FileNotFoundError(f"SSL private key not found: {ssl_keyfile}")
        return {
            "ssl_certfile": ssl_certfile,
            "ssl_keyfile": ssl_keyfile,
        }

    # Auto-generate certificates if cert_dir is provided
    if ssl_cert_dir:
        cert_path, key_path = ensure_certificates(ssl_cert_dir)
        return {
            "ssl_certfile": cert_path,
            "ssl_keyfile": key_path,
        }

    raise ValueError(
        "SSL is enabled but no certificate paths or cert_dir provided. "
        "Set HSYNC_SSL_CERTFILE and HSYNC_SSL_KEYFILE, or HSYNC_SSL_CERT_DIR."
    )
