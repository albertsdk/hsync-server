"""Tests for the CLI entrypoint (hsync_server.__main__)."""

import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from hsync_server import __main__ as cli
from hsync_server.constants import HSYNC_VERSION

runner = CliRunner()


def _patches(*, reload: bool, workers: int):
    """Common patch context for invoking main() without starting a server."""
    return (
        patch.object(cli.uvicorn, "run"),
        patch.object(cli, "setup_logging", return_value=MagicMock()),
        patch.object(cli, "get_ssl_config", return_value={}),
        patch.object(cli, "get_reload", return_value=reload),
        patch.object(cli, "get_workers", return_value=workers),
    )


class TestWorkersReloadParity:
    def test_workers_honored_when_reload_off(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=3)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, [])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["reload"] is False
        assert kwargs["workers"] == 3

    def test_workers_forced_none_when_reload_on(self):
        """reload and workers>1 are mutually exclusive in uvicorn."""
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=True, workers=4)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, [])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["reload"] is True
        assert kwargs["workers"] is None


class TestStartupOptions:
    def test_custom_port_passed_to_uvicorn(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, ["--port", "9123"])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 9123

    def test_default_host_is_all_interfaces(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, [])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "0.0.0.0"

    def test_custom_host_passed_to_uvicorn(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, ["--host", "127.0.0.1"])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"

    def test_host_port_shorthand_sets_host_and_port(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, ["--host", "127.0.0.1:9124"])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 9124

    def test_ipv6_host_is_passed_unbracketed(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, ["--host", "[::1]:9124"])
        assert result.exit_code == 0
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "::1"
        assert kwargs["port"] == 9124

    def test_host_port_and_port_flag_are_mutually_exclusive(self):
        run_p, log_p, ssl_p, reload_p, workers_p = _patches(reload=False, workers=1)
        with run_p as mock_run, log_p, ssl_p, reload_p, workers_p:
            result = runner.invoke(cli.app, ["--host", "127.0.0.1:9124", "--port", "9124"])
        assert result.exit_code != 0
        assert "not both" in result.output
        mock_run.assert_not_called()

    def test_ssl_enabled_branch(self):
        run_p, log_p, _, reload_p, workers_p = _patches(reload=False, workers=1)
        with (
            run_p as mock_run,
            log_p,
            patch.object(cli, "get_ssl_config", return_value={"ssl_certfile": "/c.pem"}),
            reload_p,
            workers_p,
        ):
            result = runner.invoke(cli.app, ["--ssl"])
        assert result.exit_code == 0
        mock_run.assert_called_once()


class TestVersionAndEnvFile:
    def test_console_script_targets_typer_app_wrapper(self):
        """The installed console script must invoke Typer, not the command body directly."""
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        assert data["project"]["scripts"]["hsync-server"] == "hsync_server.__main__:run"

    def test_version_flag(self):
        result = runner.invoke(cli.app, ["--version"])
        assert result.exit_code == 0
        assert HSYNC_VERSION in result.stdout

    def test_missing_env_file_exits_with_error(self):
        result = runner.invoke(cli.app, ["--env-file", "/no/such/file.env"])
        assert result.exit_code == 1

    def test_invalid_log_level_rejected(self):
        result = runner.invoke(cli.app, ["--log-level", "bogus"])
        assert result.exit_code != 0


class TestAdminBucketCli:
    def test_bucket_create_creates_directory_and_owner(self):
        """`admin bucket create` creates the bucket dir and assigns the owner."""
        from hsync_server.auth_store import BOOTSTRAP_ADMIN_ID, get_auth_store
        from hsync_server.config import get_buckets_path

        result = runner.invoke(cli.app, ["admin", "bucket", "create", "--bucket", "photos"])
        assert result.exit_code == 0, result.output
        assert "Bucket created: photos" in result.stdout

        from pathlib import Path

        assert (Path(get_buckets_path()) / "photos").is_dir()
        store = get_auth_store()
        assert store.get_owner("photos").id == BOOTSTRAP_ADMIN_ID  # type: ignore[union-attr]

    def test_bucket_create_duplicate_fails(self):
        """Creating an existing bucket fails with exit code 1."""
        runner.invoke(cli.app, ["admin", "bucket", "create", "--bucket", "photos"])
        result = runner.invoke(cli.app, ["admin", "bucket", "create", "--bucket", "photos"])
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_bucket_delete_removes_directory_and_rows(self):
        """`admin bucket delete` removes the bucket dir and its DB rows."""
        from pathlib import Path

        from hsync_server.auth_store import get_auth_store
        from hsync_server.config import get_buckets_path

        runner.invoke(cli.app, ["admin", "bucket", "create", "--bucket", "photos"])
        result = runner.invoke(cli.app, ["admin", "bucket", "delete", "--bucket", "photos"])
        assert result.exit_code == 0, result.output
        assert "Bucket deleted: photos" in result.stdout

        assert not (Path(get_buckets_path()) / "photos").exists()
        store = get_auth_store()
        assert store.get_owner("photos") is None

    def test_bucket_delete_missing_fails(self):
        """Deleting a non-existent bucket fails with exit code 1."""
        result = runner.invoke(cli.app, ["admin", "bucket", "delete", "--bucket", "nope"])
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_bucket_delete_db_only_removes_db_rows_without_dir(self):
        """`--db-only` removes the DB rows of a ghost bucket (no dir on disk)."""
        import sqlite3

        from hsync_server.auth_store import get_auth_store
        from hsync_server.config import get_auth_db_path

        # Simulate a "ghost" bucket: DB rows exist, directory does not.
        store = get_auth_store()
        with store._lock:
            store._begin()
            store.conn.execute(
                "INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')",
                ("ghostbucket", "HsyncAdminBootstrap01"),
            )
            store._commit()

        result = runner.invoke(
            cli.app, ["admin", "bucket", "delete", "--bucket", "ghostbucket", "--db-only"]
        )
        assert result.exit_code == 0, result.output
        assert "Bucket deleted: ghostbucket" in result.stdout

        with sqlite3.connect(get_auth_db_path()) as conn:
            row = conn.execute(
                "SELECT 1 FROM bucket_members WHERE bucket = ?", ("ghostbucket",)
            ).fetchone()
        assert row is None

    def test_bucket_delete_without_db_only_fails_on_ghost(self):
        """Plain delete keeps rejecting ghosts with exit code 1 (REST parity)."""
        result = runner.invoke(cli.app, ["admin", "bucket", "delete", "--bucket", "ghostbucket"])
        assert result.exit_code == 1
        assert "Error:" in result.output


class TestNoRealRepoStateIsolation:
    def test_real_state_lies_outside_tmp_fixtures(self, tmp_path):
        """The repo's real state/buckets dirs are never the tmp isolation dirs."""
        from tests.conftest import _REAL_BUCKETS_DIR, _REAL_STATE_DIR

        db_path = str(_REAL_STATE_DIR / "server")
        assert tmp_path not in _REAL_STATE_DIR.parents
        assert _REAL_STATE_DIR.is_dir() or not _REAL_STATE_DIR.exists()
        assert _REAL_BUCKETS_DIR.is_dir() or not _REAL_BUCKETS_DIR.exists()
        assert len(db_path) > 0


class TestFastApiApplication:
    def test_files_package_reexports_endpoint_functions(self):
        """The files package exports endpoint functions used by compatibility imports."""
        from hsync_server.api import files
        from hsync_server.api.files.delete import delete_file
        from hsync_server.api.files.download import download_file
        from hsync_server.api.files.list import get_files, get_tombstones, get_upload_status
        from hsync_server.api.files.upload import upload_file

        assert files.upload_file is upload_file
        assert files.download_file is download_file
        assert files.get_files is get_files
        assert files.get_tombstones is get_tombstones
        assert files.get_upload_status is get_upload_status
        assert files.delete_file is delete_file

    @pytest.mark.asyncio
    async def test_lifespan_preloads_hashes_from_configured_bucket_path(
        self, tmp_path, monkeypatch
    ):
        """Lifespan bootstraps admin and preloads hashes from the configured bucket path."""
        from hsync_common.models.sqlstore import reset_state_db_cache

        from hsync_server import main as app_main
        from hsync_server.auth_store import reset_auth_cache

        calls = {}

        def fake_set_sha256_hashes(path: str) -> int:
            calls["path"] = path
            return 3

        monkeypatch.setattr(app_main, "get_buckets_path", lambda: str(tmp_path))
        monkeypatch.setattr(app_main, "set_sha256_hashes", fake_set_sha256_hashes)
        monkeypatch.setattr("hsync_server.config.get_state_dir", lambda: str(tmp_path / "_state"))
        reset_auth_cache()
        reset_state_db_cache()

        try:
            async with app_main.lifespan(FastAPI()):
                assert calls["path"] == str(tmp_path)
        finally:
            reset_auth_cache()
            reset_state_db_cache()


class TestTlsFingerprint:
    """`hsync-server admin tls fingerprint` prints the persisted server pin."""

    def test_prints_the_certificate_fingerprint(self, tmp_path, monkeypatch):
        from hsync_common.ssl_utils import fingerprint_from_der

        from hsync_server import config as server_config
        from hsync_server.ssl_utils import ensure_certificates

        cert_path, _ = ensure_certificates(str(tmp_path))
        monkeypatch.setattr(server_config, "get_ssl_enabled", lambda: True)
        monkeypatch.setattr(server_config, "get_ssl_certfile", lambda: "")
        monkeypatch.setattr(server_config, "get_ssl_cert_dir", lambda: str(tmp_path))

        result = runner.invoke(cli.app, ["admin", "tls", "fingerprint"])
        assert result.exit_code == 0, result.output
        import ssl as ssl_module

        with open(cert_path, encoding="ascii") as cert_file:
            expected = fingerprint_from_der(ssl_module.PEM_cert_to_DER_cert(cert_file.read()))
        assert result.output.strip() == f"sha256:{expected}"

    def test_json_output(self, tmp_path, monkeypatch):
        import json as json_module

        from hsync_server import config as server_config
        from hsync_server.ssl_utils import ensure_certificates

        ensure_certificates(str(tmp_path))
        monkeypatch.setattr(server_config, "get_ssl_enabled", lambda: True)
        monkeypatch.setattr(server_config, "get_ssl_certfile", lambda: "")
        monkeypatch.setattr(server_config, "get_ssl_cert_dir", lambda: str(tmp_path))

        result = runner.invoke(cli.app, ["admin", "tls", "fingerprint", "--json"])
        assert result.exit_code == 0, result.output
        payload = json_module.loads(result.output)
        assert payload["fingerprint"].startswith("sha256:")
        assert payload["certificate"].endswith("server.crt")

    def test_rotate_changes_the_fingerprint(self, tmp_path, monkeypatch):
        from hsync_server import config as server_config
        from hsync_server.ssl_utils import ensure_certificates

        ensure_certificates(str(tmp_path))
        monkeypatch.setattr(server_config, "get_ssl_enabled", lambda: True)
        monkeypatch.setattr(server_config, "get_ssl_certfile", lambda: "")
        monkeypatch.setattr(server_config, "get_ssl_keyfile", lambda: "")
        monkeypatch.setattr(server_config, "get_ssl_cert_dir", lambda: str(tmp_path))

        before = runner.invoke(cli.app, ["admin", "tls", "fingerprint"])
        assert before.exit_code == 0, before.output

        result = runner.invoke(cli.app, ["admin", "tls", "rotate"])
        assert result.exit_code == 0, result.output
        assert "New fingerprint: sha256:" in result.output

        after = runner.invoke(cli.app, ["admin", "tls", "fingerprint"])
        assert after.exit_code == 0, after.output
        assert after.output.strip() != before.output.strip()

    def test_tls_disabled_exits_with_error(self, monkeypatch):
        from hsync_server import config as server_config

        monkeypatch.setattr(server_config, "get_ssl_enabled", lambda: False)
        result = runner.invoke(cli.app, ["admin", "tls", "fingerprint"])
        assert result.exit_code == 1
        assert "TLS is disabled" in result.output
