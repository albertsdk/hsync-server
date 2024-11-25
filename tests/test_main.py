"""Tests for the CLI entrypoint (hsync_server.__main__)."""

from unittest.mock import MagicMock, patch

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
