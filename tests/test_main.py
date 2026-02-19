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
