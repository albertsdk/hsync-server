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

Usage:
    from hsync_server.ssl_utils import ensure_certificates, get_ssl_config

    # Ensure certificates exist (generates if missing)
    cert_file, key_file = ensure_certificates()

    # Get SSL configuration for uvicorn
    ssl_config = get_ssl_config()
"""

import ipaddress
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from loguru import logger as log

# Default certificate validity period (365 days)
DEFAULT_CERT_VALIDITY_DAYS = 365

# Default RSA key size (2048 bits for good security/performance balance)
DEFAULT_KEY_SIZE = 2048


def generate_self_signed_cert(
    cert_path: str,
    key_path: str,
    hostname: str | None = None,
    validity_days: int = DEFAULT_CERT_VALIDITY_DAYS,
    key_size: int = DEFAULT_KEY_SIZE,
) -> tuple[str, str]:
    """
    Generate a self-signed certificate and private key.

    Args:
        cert_path: Path where the certificate will be saved.
        key_path: Path where the private key will be saved.
        hostname: Hostname for the certificate (defaults to system hostname).
        validity_days: Certificate validity period in days.
        key_size: RSA key size in bits.

    Returns:
        Tuple of (cert_path, key_path).

    Raises:
        OSError: If unable to write certificate files.
    """
    if hostname is None:
        hostname = socket.gethostname()

    log.info(f"Generating self-signed certificate for hostname: {hostname}")

    # Generate RSA private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )

    # Build certificate subject and issuer (same for self-signed)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "IT"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Italy"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSYNC"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Self-Signed"),
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )

    # Calculate validity period
    now = datetime.now(UTC)
    not_valid_before = now
    not_valid_after = now + timedelta(days=validity_days)

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

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Ensure parent directories exist
    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)
    Path(key_path).parent.mkdir(parents=True, exist_ok=True)

    # Write private key (with restrictive permissions)
    with open(key_path, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(key_path, 0o600)  # Read/write only for owner

    # Write certificate
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(cert_path, 0o644)  # Readable by all

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
    cert_path = os.path.join(cert_dir, cert_filename)
    key_path = os.path.join(cert_dir, key_filename)

    if os.path.exists(cert_path) and os.path.exists(key_path):
        log.info(f"Using existing certificates from: {cert_dir}")
        return cert_path, key_path

    log.info("Certificates not found, generating new self-signed certificates...")
    return generate_self_signed_cert(cert_path, key_path, hostname)


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
        Dictionary with ssl_keyfile and ssl_certfile keys (or empty if disabled).
    """
    if not ssl_enabled:
        return {}

    # If explicit paths provided, use them
    if ssl_certfile and ssl_keyfile:
        if not os.path.exists(ssl_certfile):
            raise FileNotFoundError(f"SSL certificate not found: {ssl_certfile}")
        if not os.path.exists(ssl_keyfile):
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
