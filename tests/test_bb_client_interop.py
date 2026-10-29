# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box pilot: drive the *real hsync-client CLI* from the server repo.

This is the counterpart of the client's ``blackbox`` group (which depends on
hsync-server to spawn a live server). Installing hsync-server with
``poetry install --with blackbox`` brings hsync-client as a sibling path-dep,
so these tests can invoke the real client binary and verify the public
contract across repository boundaries.

Requires HSYNC_BB_SERVER=1 and the optional blackbox group installed.
"""

import contextlib
import datetime
import os
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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


def _raw_client_env() -> dict[str, str]:
    """Process environment scrubbed of every ``HSYNC_*`` variable."""
    return {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}


def _server_url(base_url: str) -> str:
    """Turn a live-server base URL into the bare ``HOST:PORT`` the client
    ``server`` subcommand accepts via ``--server-url`` (normalized to
    ``https://``)."""
    parts = urlsplit(base_url)
    assert parts.hostname is not None, f"unexpected base_url: {base_url}"
    return f"{parts.hostname}:{parts.port or 9753}"


@contextlib.contextmanager
def _tls_server(tmp_path_factory, bucket: str):
    """Start a TLS live server with a self-signed cert and create a bucket.

    Uses the shared ``_start_live_server`` harness (scrubbed env, log to
    file, ``/health`` readiness) with SSL enabled via env overrides — never
    a hand-rolled ``subprocess.Popen`` with an unscrubbed environment.
    Yields ``(base_url, api_key, bucket)``.
    """
    from tests.conftest import (
        BB_API_KEY,
        _discard_file,
        _start_live_server,
        _stop_proc,
    )

    # Given a self-signed TLS certificate
    cert_dir = tmp_path_factory.mktemp("bbssl")
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
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    # When the server is started with SSL via the shared harness
    proc, base_url, buckets_dir, log_path = _start_live_server(
        env_overrides={
            "HSYNC_SSL_ENABLED": "True",
            "HSYNC_SSL_CERTFILE": str(cert_path),
            "HSYNC_SSL_KEYFILE": str(key_path),
        }
    )
    try:
        # Then the bucket can be created on the live TLS server
        r = httpx.post(
            f"{base_url}/api/v1/buckets",
            params={"bucket": bucket},
            headers={"Authorization": f"Bearer {BB_API_KEY}"},
            verify=False,
            timeout=15.0,
        )
        assert r.status_code == 201, r.text
        yield base_url, BB_API_KEY, bucket
    finally:
        _stop_proc(proc)
        shutil.rmtree(buckets_dir, ignore_errors=True)
        shutil.rmtree(buckets_dir + "_state", ignore_errors=True)
        _discard_file(log_path)


# Subcommands the server-side tooling relies on. The check is a subset test on
# purpose: the client owns its CLI surface (SSOT), so a subcommand *added* there
# must never fail a sibling repository's interop suite, while a removed or
# renamed one still does.
REQUIRED_CLIENT_SUBCOMMANDS = {"p2p", "server", "state", "trust", "derive-group-key"}


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
        assert "subcommands" in doc, doc
        advertised = set(doc["subcommands"])
        assert advertised >= REQUIRED_CLIENT_SUBCOMMANDS, REQUIRED_CLIENT_SUBCOMMANDS - advertised
        assert "cli_options" in doc, doc
        cli_options = doc["cli_options"]
        assert isinstance(cli_options, dict), cli_options
        assert set(cli_options) >= REQUIRED_CLIENT_SUBCOMMANDS, cli_options
        # Every supported CLI surface is advertised; the conflict model is part
        # of the protocol capability set shared with the server (SSOT in
        # hsync-common). Sanity-check a few stable well-known flags.
        for known in ("--bucket", "--ca-cert", "--insecure"):
            assert known in cli_options["server"], f"{known} missing from server cli_options"


class TestClientUploadArbitrationE2E:
    """A real client one-shot sync against a TLS live server (the client
    hard-refuses to send the API key over plain HTTP, so the server is
    started with a self-signed cert and the client is driven with
    ``--insecure``)."""

    @pytest.fixture
    def tls_server(self, tmp_path_factory):
        with _tls_server(tmp_path_factory, "bb-interop") as ctx:
            yield ctx

    def test_client_uploads_new_file_to_server(self, tls_server, tmp_path):
        """A real client one-shot sync pushes a local file to the TLS server.

        Exercises the exact wire contract the client's own black-box suite
        uses, mirrored here: the server arbitrates the upload causally and the
        file appears in the listing.
        """
        base_url, api_key, bucket = tls_server
        (tmp_path / "from-real-client.txt").write_bytes(b"client-authored\n")

        env = _raw_client_env()
        # Client env: hsync-client still reads HSYNC_API_KEY and sends its value
        # verbatim as the Bearer token (the full hs_HsyncAdminRendezvous01_<secret>).
        env["HSYNC_API_KEY"] = api_key
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "server",
                "--server-url",
                _server_url(base_url),
                "-b",
                bucket,
                "-p",
                str(tmp_path),
                "--allow-deletions",
                "--insecure",
                "--min-free-disk-space",
                "0",
                "--once",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr

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


class TestTwoClientSyncE2E:
    """Two real client one-shot syncs converging through the same TLS server.

    Each client owns a separate local directory (separate folder UUID, like
    two real machines): the server is their only channel. The scenario walks
    the full lifecycle — initial file + nested-directory replication, then a
    rename and a delete propagated to the second client.
    """

    @pytest.fixture
    def server(self, tmp_path_factory):
        bucket = f"bb-twoclient-{uuid.uuid4().hex[:8]}"
        with _tls_server(tmp_path_factory, bucket) as ctx:
            yield ctx

    def _run_sync(
        self, base_url: str, api_key: str, bucket: str, path
    ) -> subprocess.CompletedProcess[str]:
        """One-shot sync of *path* against the server; returns the CLI result."""
        env = _raw_client_env()
        env["HSYNC_API_KEY"] = api_key
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "server",
                "--server-url",
                _server_url(base_url),
                "-b",
                bucket,
                "-p",
                str(path),
                "--allow-deletions",
                "--insecure",
                "--min-free-disk-space",
                "0",
                "--once",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def _server_files(self, base_url: str, api_key: str, bucket: str) -> set[str]:
        with httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=False,
        ) as client:
            r = client.get(f"/api/v1/buckets/{bucket}/files")
            assert r.status_code == 200, r.text
            return {f["filepath"] for f in r.json()}

    def test_two_clients_sync_file_directory_rename_delete(self, server, tmp_path):
        base_url, api_key, bucket = server

        # Phase 1 — client A authors a file and a nested-directory file.
        dir_a = tmp_path / "client-a"
        dir_b = tmp_path / "client-b"
        (dir_a / "sub").mkdir(parents=True)
        (dir_a / "hello.txt").write_text("hello from client A\n", encoding="utf-8")
        (dir_a / "sub" / "inner.txt").write_text("nested content\n", encoding="utf-8")

        result = self._run_sync(base_url, api_key, bucket, dir_a)
        assert result.returncode == 0, result.stdout + result.stderr

        server_files = self._server_files(base_url, api_key, bucket)
        assert "hello.txt" in server_files
        assert "sub/inner.txt" in server_files

        # Phase 2 — client B, starting from an empty directory, converges via
        # the server: the file and the nested directory structure are created.
        dir_b.mkdir()
        result = self._run_sync(base_url, api_key, bucket, dir_b)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (dir_b / "hello.txt").read_text(encoding="utf-8") == "hello from client A\n"
        assert (dir_b / "sub" / "inner.txt").read_text(encoding="utf-8") == "nested content\n"

        # Phase 3 — rename on client A propagates to client B.
        os.rename(dir_a / "hello.txt", dir_a / "renamed.txt")
        result = self._run_sync(base_url, api_key, bucket, dir_a)
        assert result.returncode == 0, result.stdout + result.stderr
        result = self._run_sync(base_url, api_key, bucket, dir_b)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (dir_b / "hello.txt").exists()
        assert (dir_b / "renamed.txt").read_text(encoding="utf-8") == "hello from client A\n"
        server_files = self._server_files(base_url, api_key, bucket)
        assert "renamed.txt" in server_files and "hello.txt" not in server_files

        # Phase 4 — delete on client A propagates to client B.
        os.remove(dir_a / "sub" / "inner.txt")
        result = self._run_sync(base_url, api_key, bucket, dir_a)
        assert result.returncode == 0, result.stdout + result.stderr
        result = self._run_sync(base_url, api_key, bucket, dir_b)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (dir_b / "sub" / "inner.txt").exists()
        server_files = self._server_files(base_url, api_key, bucket)
        assert "sub/inner.txt" not in server_files
        assert "renamed.txt" in server_files
