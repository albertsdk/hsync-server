# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# Tests for SSL/TLS utilities module.

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hsync_server.ssl_utils import (
    DEFAULT_CERT_VALIDITY_DAYS,
    ensure_certificates,
    generate_self_signed_cert,
    get_ssl_config,
)


class TestGenerateSelfSignedCert:
    """Tests for generate_self_signed_cert function."""

    def test_generates_certificate_and_key(self, tmp_path):
        """Test that certificate and key files are created."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        result_cert, result_key = generate_self_signed_cert(cert_path, key_path)

        assert result_cert == cert_path
        assert result_key == key_path
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)

    def test_certificate_is_valid_pem(self, tmp_path):
        """Test that generated certificate is valid PEM format."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)
        assert cert is not None

    def test_private_key_is_unencrypted_ecdsa_pkcs8(self, tmp_path):
        """The key is an unencrypted ECDSA P-256 PKCS#8 (ADR-0037)."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        with open(key_path, "rb") as f:
            key_data = f.read()

        assert key_data.startswith(b"-----BEGIN PRIVATE KEY-----")
        key = serialization.load_pem_private_key(key_data, password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_certificate_has_correct_hostname(self, tmp_path):
        """Test that certificate has the specified hostname."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")
        hostname = "test-server.local"

        generate_self_signed_cert(cert_path, key_path, hostname=hostname)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0]
        assert cn.value == hostname

    def test_certificate_has_san_extension(self, tmp_path):
        """Test that certificate has Subject Alternative Names."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path, hostname="testhost")

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "testhost" in dns_names
        assert "localhost" in dns_names

    def test_certificate_uses_ecdsa_p256_signature(self, tmp_path):
        """The certificate is signed with ECDSA P-256 (ADR-0037)."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
        assert isinstance(cert.public_key().curve, ec.SECP256R1)

    def test_key_file_has_restrictive_permissions(self, tmp_path):
        """Test that private key file has 0600 permissions."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        key_stat = os.stat(key_path)
        assert (key_stat.st_mode & 0o777) == 0o600

    def test_cert_file_has_readable_permissions(self, tmp_path):
        """Test that certificate file has 0644 permissions."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        cert_stat = os.stat(cert_path)
        assert (cert_stat.st_mode & 0o777) == 0o644

    def test_creates_parent_directories(self, tmp_path):
        """Test that parent directories are created if they don't exist."""
        cert_path = str(tmp_path / "subdir" / "deep" / "server.crt")
        key_path = str(tmp_path / "subdir" / "deep" / "server.key")

        generate_self_signed_cert(cert_path, key_path)

        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)

    def test_custom_validity_days(self, tmp_path):
        """Test certificate with custom validity period."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        generate_self_signed_cert(cert_path, key_path, validity_days=30)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        # The span is validity_days plus the 1-day NotBefore backdate (clock skew).
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert 30 <= delta.days <= 32

    def test_uses_system_hostname_by_default(self, tmp_path):
        """Test that system hostname is used when not specified."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        with patch("socket.gethostname", return_value="mocked-hostname"):
            generate_self_signed_cert(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0]
        assert cn.value == "mocked-hostname"


class TestEnsureCertificates:
    """Tests for ensure_certificates function."""

    def test_generates_certificates_if_not_exist(self, tmp_path):
        """Test that certificates are generated when they don't exist."""
        cert_dir = str(tmp_path / "certs")

        cert_path, key_path = ensure_certificates(cert_dir)

        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)
        assert cert_path.endswith("server.crt")
        assert key_path.endswith("server.key")

    def test_uses_existing_certificates(self, tmp_path):
        """Test that existing certificates are reused."""
        cert_dir = str(tmp_path / "certs")
        os.makedirs(cert_dir)

        cert_path = os.path.join(cert_dir, "server.crt")
        key_path = os.path.join(cert_dir, "server.key")
        Path(cert_path).write_text("existing cert")
        Path(key_path).write_text("existing key")

        result_cert, result_key = ensure_certificates(cert_dir)

        assert result_cert == cert_path
        assert result_key == key_path
        assert Path(cert_path).read_text() == "existing cert"

    def test_custom_filenames(self, tmp_path):
        """Test with custom certificate filenames."""
        cert_dir = str(tmp_path / "certs")

        cert_path, key_path = ensure_certificates(
            cert_dir,
            cert_filename="custom.crt",
            key_filename="custom.key",
        )

        assert cert_path.endswith("custom.crt")
        assert key_path.endswith("custom.key")


class TestGetSslConfig:
    """Tests for get_ssl_config function."""

    def test_returns_empty_dict_when_disabled(self):
        """Test that empty dict is returned when SSL is disabled."""
        config = get_ssl_config(ssl_enabled=False)
        assert config == {}

    def test_uses_explicit_paths(self, tmp_path):
        """Test that explicit certificate paths are used."""
        cert_path = str(tmp_path / "server.crt")
        key_path = str(tmp_path / "server.key")

        Path(cert_path).write_text("cert")
        Path(key_path).write_text("key")

        config = get_ssl_config(
            ssl_enabled=True,
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )

        assert config["ssl_certfile"] == cert_path
        assert config["ssl_keyfile"] == key_path

    def test_raises_error_if_certfile_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing certfile."""
        with pytest.raises(FileNotFoundError, match="SSL certificate not found"):
            get_ssl_config(
                ssl_enabled=True,
                ssl_certfile="/nonexistent/cert.pem",
                ssl_keyfile="/nonexistent/key.pem",
            )

    def test_raises_error_if_keyfile_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing keyfile."""
        cert_path = str(tmp_path / "server.crt")
        Path(cert_path).write_text("cert")

        with pytest.raises(FileNotFoundError, match="SSL private key not found"):
            get_ssl_config(
                ssl_enabled=True,
                ssl_certfile=cert_path,
                ssl_keyfile="/nonexistent/key.pem",
            )

    def test_auto_generates_certificates(self, tmp_path):
        """Test that certificates are auto-generated when cert_dir is provided."""
        cert_dir = str(tmp_path / "certs")

        config = get_ssl_config(ssl_enabled=True, ssl_cert_dir=cert_dir)

        assert "ssl_certfile" in config
        assert "ssl_keyfile" in config
        assert os.path.exists(config["ssl_certfile"])
        assert os.path.exists(config["ssl_keyfile"])

    def test_raises_error_if_no_paths_provided(self):
        """Test that ValueError is raised when SSL enabled but no paths."""
        with pytest.raises(ValueError, match="SSL is enabled but no certificate"):
            get_ssl_config(ssl_enabled=True)


class TestConstants:
    """Tests for module constants."""

    def test_default_validity_days_is_twenty_years(self):
        """Test the shared service-identity validity (20 years, ADR-0037)."""
        assert DEFAULT_CERT_VALIDITY_DAYS == 7305
