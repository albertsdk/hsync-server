# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box pilot: drive the *real hsync-client CLI* from the server repo.

This is the counterpart of the client's ``blackbox`` group (which depends on
hsync-server to spawn a live server). Installing hsync-server with
``poetry install --with blackbox`` brings hsync-client as a sibling path-dep,
so these tests can invoke the real client binary and verify the public
contract across repository boundaries.

Requires HSYNC_BB_SERVER=1 and the optional blackbox group installed.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"


def _client_available() -> bool:
    """True when the client module is importable (blackbox group installed)."""
    try:
        import hsync_client  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.skipif(
        not _client_available(),
        reason="hsync-client not installed (poetry install --with blackbox)",
    ),
]


class TestClientCliCapabilities:
    def test_client_capabilities_json_is_parseable(self):
        """The real client CLI emits a capabilities document the server-side
        tooling can consume (the desktop gate relies on the same document)."""
        result = subprocess.run(
            [sys.executable, "-m", "hsync_client", "--capabilities-json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

        import json

        doc = json.loads(result.stdout)
        assert "cli_options" in doc, doc
        cli_options = doc["cli_options"]
        assert isinstance(cli_options, list), cli_options
        # Every supported CLI surface is advertised; the conflict model is part
        # of the protocol capability set shared with the server (SSOT in
        # hsync-common). Sanity-check a few stable well-known flags.
        for known in ("--bucket", "--ca-cert", "--insecure"):
            assert known in cli_options, f"{known} missing from cli_options"


class TestClientUploadArbitrationE2E:
    """A real client one-shot sync against a TLS live server (the client
    hard-refuses to send the API key over plain HTTP, so the server is
    started with a self-signed cert and the client is driven with
    ``--insecure``)."""

    # Fixture-local TLS server.
    @pytest.fixture
    def tls_server(self, tmp_path_factory):
        from tests.conftest import BB_ADMIN_SECRET, BB_API_KEY

        buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_interop_")
        cert_dir = tmp_path_factory.mktemp("bbssl")

        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        cert_path = cert_dir / "cert.pem"
        key_path = cert_dir / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.BestAvailableEncryption(b"test-bb-interop-ssl-password"),
            )
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
        env.update(
            {
                # The server reads the 64-hex bootstrap secret from
                # HSYNC_ADMIN_KEY; the Bearer token (BB_API_KEY) is derived
                # from it and is what the *client* sends.
                "HSYNC_ADMIN_KEY": BB_ADMIN_SECRET,
                "HSYNC_SCRYPT_N": "16",
                "HSYNC_SCRYPT_MAXMEM": "33554432",
                "HSYNC_BUCKETS": buckets_dir,
                "HSYNC_STATE_DIR": buckets_dir + "_state",
                "HSYNC_RATE_LIMIT_DEFAULT": "1000000/minute",
                "HSYNC_RATE_LIMIT_UPLOAD": "1000000/minute",
                "HSYNC_MIN_FREE_DISK_SPACE": "0",
                "HSYNC_SSL_KEYFILE_PASSWORD": "test-bb-interop-ssl-password",
            }
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_server",
                "--port",
                str(port),
                "--ssl",
                "--ssl-cert",
                str(cert_path),
                "--ssl-key",
                str(key_path),
                "--log-level",
                "warning",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for health.
        import httpx

        base_url = f"https://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early ({proc.returncode})")
            try:
                r = httpx.get(f"{base_url}/health", verify=False, timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.15)
        else:
            raise RuntimeError("server did not become healthy")

        # Create a bucket via REST.
        name = "bb-interop"
        r = httpx.post(
            f"{base_url}/api/v1/buckets",
            params={"bucket": name},
            headers={"Authorization": f"Bearer {BB_API_KEY}"},
            verify=False,
            timeout=15.0,
        )
        assert r.status_code == 201, r.text

        try:
            yield base_url, BB_API_KEY, name
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            shutil.rmtree(buckets_dir, ignore_errors=True)

    def test_client_uploads_new_file_to_server(self, tls_server, tmp_path):
        """A real client one-shot sync pushes a local file to the TLS server.

        Exercises the exact wire contract the client's own black-box suite
        uses, mirrored here: the server arbitrates the upload causally and the
        file appears in the listing.
        """
        base_url, api_key, bucket = tls_server
        (tmp_path / "from-real-client.txt").write_bytes(b"client-authored\n")

        env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
        # Client env: hsync-client still reads HSYNC_API_KEY and sends its value
        # verbatim as the Bearer token (the full hs_HsyncAdminBootstrap01_<secret>).
        env["HSYNC_API_KEY"] = api_key
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "-b",
                bucket,
                "-s",
                base_url,
                "-p",
                str(tmp_path),
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        import httpx

        with httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=False,
        ) as client:
            r = client.get(f"/api/v1/buckets/{bucket}/files")
            assert r.status_code == 200
            names = {f["filepath"] for f in r.json()}
            assert "from-real-client.txt" in names, names
