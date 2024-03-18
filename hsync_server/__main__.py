"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.
"""

import sys
from typing import Optional

import typer
import uvicorn
from typing_extensions import Annotated

from hsync_server.config import (
    get_log_level,
    get_port,
    get_ssl_certfile,
    get_ssl_cert_dir,
    get_ssl_enabled,
    get_ssl_keyfile,
    load_env_file,
)
from hsync_server.constants import HSYNC_VERSION
from hsync_server.logger import setup_logging, VALID_LOG_LEVELS
from hsync_server.ssl_utils import get_ssl_config


# =============================================================================
# CLI Application
# =============================================================================

app = typer.Typer(
    name="hsync-server",
    help="HSYNC Server - HTTP-based file synchronization server",
    add_completion=False,
    rich_markup_mode="rich",  # Enable rich formatting for grouped options
)


def _version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        print(f"hsync-server {HSYNC_VERSION}")
        raise typer.Exit()


def _validate_log_level(value: Optional[str]) -> Optional[str]:
    """Validate log level against valid options."""
    if value is not None and value not in VALID_LOG_LEVELS:
        raise typer.BadParameter(
            f"Invalid log level '{value}'. Must be one of: {', '.join(VALID_LOG_LEVELS)}"
        )
    return value


@app.command()
def main(
    # -------------------------------------------------------------------------
    # Server options
    # -------------------------------------------------------------------------
    port: Annotated[Optional[int], typer.Option(
        "-p", "--port",
        help="Server port (default: 8765).",
        metavar="PORT",
        rich_help_panel="Server",
    )] = None,
    log_level: Annotated[Optional[str], typer.Option(
        "--log-level",
        help="Log level: debug, info, warning, error (default: info).",
        metavar="LEVEL",
        callback=lambda v: _validate_log_level(v),
        rich_help_panel="Server",
    )] = None,
    no_color: Annotated[bool, typer.Option(
        "--no-color",
        help="Disable colored log output.",
        rich_help_panel="Server",
    )] = False,
    # -------------------------------------------------------------------------
    # SSL/TLS options
    # -------------------------------------------------------------------------
    ssl_enabled: Annotated[bool, typer.Option(
        "--ssl",
        help="Enable HTTPS/WSS. Auto-generates certificates if not provided.",
        rich_help_panel="SSL/TLS",
    )] = False,
    ssl_certfile: Annotated[Optional[str], typer.Option(
        "--ssl-cert",
        help="Path to SSL certificate file (PEM format).",
        metavar="FILE",
        rich_help_panel="SSL/TLS",
    )] = None,
    ssl_keyfile: Annotated[Optional[str], typer.Option(
        "--ssl-key",
        help="Path to SSL private key file (PEM format).",
        metavar="FILE",
        rich_help_panel="SSL/TLS",
    )] = None,
    ssl_cert_dir: Annotated[Optional[str], typer.Option(
        "--ssl-cert-dir",
        help="Directory for auto-generated certificates (default: ./certs).",
        metavar="DIR",
        rich_help_panel="SSL/TLS",
    )] = None,
    # -------------------------------------------------------------------------
    # General options
    # -------------------------------------------------------------------------
    env_file: Annotated[Optional[str], typer.Option(
        "--env-file",
        help="Load environment variables from file.",
        metavar="PATH",
    )] = None,
    version: Annotated[Optional[bool], typer.Option(
        "-V", "--version",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    )] = None,
) -> None:
    """
    HSYNC Server - HTTP-based file synchronization server.

    Central server for HSYNC clients. Manages buckets (shared directories),
    handles file uploads/downloads via REST API, and broadcasts real-time
    events to connected clients via WebSocket.

    [bold]Option Groups:[/bold]

        [cyan]Server[/cyan]      Network port and logging configuration
        [cyan]SSL/TLS[/cyan]     HTTPS/WSS encryption settings

    [bold]Examples:[/bold]

        [dim]# Start server on default port (8765)[/dim]
        $ hsync-server

        [dim]# Start server on custom port[/dim]
        $ hsync-server --port 9000

        [dim]# Start with HTTPS enabled (auto-generates certificates)[/dim]
        $ hsync-server --ssl

        [dim]# Start with custom SSL certificates[/dim]
        $ hsync-server --ssl --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem

        [dim]# Start with debug logging[/dim]
        $ hsync-server --log-level debug

        [dim]# Load configuration from environment file[/dim]
        $ hsync-server --env-file /path/to/prod.env

    [bold]Environment Variables:[/bold]

        HSYNC_API_KEY           API key for authentication (required)
        HSYNC_BUCKETS           Path to buckets directory
        HSYNC_CORS_ENABLED      Enable CORS (true/false)
        HSYNC_CORS_ORIGINS      Allowed CORS origins (comma-separated)
        HSYNC_MAX_UPLOAD_SIZE   Maximum upload size in bytes

    [bold]Authors:[/bold]

        Alberto Basso <alberto.basso@proton.me>
    """
    # Load environment file if specified (before reading any config)
    if env_file:
        try:
            load_env_file(env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)

    # Get configuration values (CLI args take precedence over env vars)
    effective_log_level = log_level if log_level else get_log_level()
    effective_port = port if port else get_port()
    effective_ssl_enabled = ssl_enabled or get_ssl_enabled()
    effective_ssl_certfile = ssl_certfile if ssl_certfile else get_ssl_certfile()
    effective_ssl_keyfile = ssl_keyfile if ssl_keyfile else get_ssl_keyfile()
    effective_ssl_cert_dir = ssl_cert_dir if ssl_cert_dir else get_ssl_cert_dir()

    # Configure logging before starting the server
    LOG = setup_logging(level=effective_log_level, no_color=no_color)

    # Log version at startup
    LOG.info(f"HSYNC Server v{HSYNC_VERSION}")

    # Get SSL configuration (empty dict if SSL disabled)
    ssl_config = get_ssl_config(
        ssl_enabled=effective_ssl_enabled,
        ssl_certfile=effective_ssl_certfile or None,
        ssl_keyfile=effective_ssl_keyfile or None,
        ssl_cert_dir=effective_ssl_cert_dir,
    )

    # Log server startup mode
    if effective_ssl_enabled:
        LOG.info(f"Starting HSYNC server with HTTPS/WSS on port {effective_port}")
        LOG.info(f"Certificate: {ssl_config.get('ssl_certfile', 'N/A')}")
    else:
        LOG.info(f"Starting HSYNC server with HTTP/WS on port {effective_port}")
        LOG.warning("SSL is disabled. Communication is NOT encrypted!")

    uvicorn.run(
        "hsync_server.main:app",
        host="0.0.0.0",
        port=effective_port,
        log_level=effective_log_level,
        reload=True,
        **ssl_config,
    )


def run() -> None:
    """Entry point for the CLI application."""
    app()


if __name__ == "__main__":
    run()

