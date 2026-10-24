# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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

    ssl_config = get_ssl_config(ssl_enabled=True, ssl_cert_dir=HSYNC_SSL_CERT_DIR)
"""

import ipaddress
import socket
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID
from hsync_common.ssl_utils import (
    PUBLIC_CERT_FILE_MODE,
    SERVICE_CERT_VALIDITY_DAYS,
    ServiceIdentity,
    create_service_identity,
)
from loguru import logger as log

# Default certificate validity: the shared service-identity lifetime (20 years).
# The certificate is authenticated by fingerprint pinning (ADR-0036/ADR-0037),
# not by WebPKI, so it lives for the deployment instead of a browser-friendly
# 398/825-day window.
DEFAULT_CERT_VALIDITY_DAYS = SERVICE_CERT_VALIDITY_DAYS


def _identity_parameters(hostname: str) -> tuple[list[x509.NameAttribute], list[x509.GeneralName]]:
    """Build the server identity's subject and SAN list for *hostname*.

    Shared by generation and load-or-create so a rotated certificate and a
    first-run certificate carry the same names.
    """
    subject_attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IT"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Italy"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Self-Signed"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ]

    # Include both hostname and localhost for local development
    san_list: list[x509.GeneralName] = [
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
    return subject_attrs, san_list


def _create_server_identity(
    cert_path: str, key_path: str, hostname: str | None, validity_days: int
) -> ServiceIdentity:
    """Generate the server keypair (always regenerating) via the shared helper."""
    resolved = hostname if hostname is not None else socket.gethostname()
    subject_attrs, san_list = _identity_parameters(resolved)
    return create_service_identity(
        cert_path,
        key_path,
        subject_attrs=subject_attrs,
        san_list=san_list,
        validity_days=validity_days,
        cert_mode=PUBLIC_CERT_FILE_MODE,
    )


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
    protected by filesystem permissions rather than a passphrase. The
    certificate is world-readable because the server must present it to clients.

    Always regenerates: this is the rotation entry point. Startup uses
    :func:`ensure_certificates`, which loads an existing pair.

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
    log.info(f"Generating self-signed certificate for hostname: {hostname or socket.gethostname()}")
    identity = _create_server_identity(cert_path, key_path, hostname, validity_days)
    log.info(f"Certificate generated: {cert_path}")
    log.info(f"Private key generated: {key_path}")
    log.info(f"Certificate valid until: {identity.not_valid_after.isoformat()}")
    return cert_path, key_path


def ensure_certificates(
    cert_dir: str,
    cert_filename: str = "server.crt",
    key_filename: str = "server.key",
    hostname: str | None = None,
) -> tuple[str, str]:
    """
    Ensure SSL certificates exist, generating them if necessary.

    An existing pair is used as-is, without validation: the files in
    ``HSYNC_SSL_CERT_DIR`` may be an operator-provided certificate (any key
    type), not only one this server generated — the TLS stack validates it at
    bind time. Generation itself is the shared
    ``hsync_common.ssl_utils.create_service_identity``.

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
        no key password is configured.
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
