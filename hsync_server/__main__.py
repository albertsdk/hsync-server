"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.
"""

import argparse
import sys

import uvicorn

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


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    from argparse import RawDescriptionHelpFormatter

    epilog = """
Examples:
  # Start server on default port (8765)
  hsync-server

  # Start server on custom port
  hsync-server --port 9000

  # Start with HTTPS enabled (auto-generates certificates)
  hsync-server --ssl

  # Start with custom SSL certificates
  hsync-server --ssl --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem

  # Start with debug logging
  hsync-server --log-level debug

  # Load configuration from environment file
  hsync-server --env-file /path/to/prod.env

Environment variables:
  HSYNC_API_KEY         API key for authentication (required)
  HSYNC_BUCKETS         Path to buckets directory
  HSYNC_CORS_ENABLED    Enable CORS (true/false)
  HSYNC_CORS_ORIGINS    Allowed CORS origins (comma-separated)
  HSYNC_MAX_UPLOAD_SIZE Maximum upload size in bytes
"""

    parser = argparse.ArgumentParser(
        prog="hsync-server",
        description="HSYNC Server - HTTP-based file synchronization server",
        formatter_class=RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument(
        "--env-file",
        type=str,
        required=False,
        metavar="PATH",
        dest="env_file",
        help="Load environment variables from file.",
    )

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {HSYNC_VERSION}",
    )

    # -------------------------------------------------------------------------
    # Server options
    # -------------------------------------------------------------------------
    server_group = parser.add_argument_group("Server")

    server_group.add_argument(
        "-p",
        "--port",
        type=int,
        required=False,
        default=None,
        metavar="PORT",
        dest="port",
        help="Server port (default: 8765).",
    )

    server_group.add_argument(
        "--log-level",
        type=str,
        required=False,
        default=None,
        choices=list(VALID_LOG_LEVELS),
        metavar="LEVEL",
        dest="log_level",
        help="Log level: debug, info, warning, error (default: info).",
    )

    # -------------------------------------------------------------------------
    # SSL/TLS options
    # -------------------------------------------------------------------------
    ssl_group = parser.add_argument_group("SSL/TLS")

    ssl_group.add_argument(
        "--ssl",
        action="store_true",
        dest="ssl_enabled",
        help="Enable HTTPS/WSS. Auto-generates certificates if not provided.",
    )

    ssl_group.add_argument(
        "--ssl-cert",
        type=str,
        required=False,
        default=None,
        metavar="FILE",
        dest="ssl_certfile",
        help="Path to SSL certificate file (PEM format).",
    )

    ssl_group.add_argument(
        "--ssl-key",
        type=str,
        required=False,
        default=None,
        metavar="FILE",
        dest="ssl_keyfile",
        help="Path to SSL private key file (PEM format).",
    )

    ssl_group.add_argument(
        "--ssl-cert-dir",
        type=str,
        required=False,
        default=None,
        metavar="DIR",
        dest="ssl_cert_dir",
        help="Directory for auto-generated certificates (default: ./certs).",
    )

    return parser


def main() -> None:
    """Run the HSYNC server using uvicorn."""
    # Parse command line arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # Load environment file if specified (before reading any config)
    if args.env_file:
        try:
            load_env_file(args.env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Get configuration values (CLI args take precedence over env vars)
    log_level = args.log_level if args.log_level else get_log_level()
    port = args.port if args.port else get_port()
    ssl_enabled = args.ssl_enabled or get_ssl_enabled()
    ssl_certfile = args.ssl_certfile if args.ssl_certfile else get_ssl_certfile()
    ssl_keyfile = args.ssl_keyfile if args.ssl_keyfile else get_ssl_keyfile()
    ssl_cert_dir = args.ssl_cert_dir if args.ssl_cert_dir else get_ssl_cert_dir()

    # Configure logging before starting the server
    LOG = setup_logging(level=log_level)

    # Log version at startup
    LOG.info(f"HSYNC Server v{HSYNC_VERSION}")

    # Get SSL configuration (empty dict if SSL disabled)
    ssl_config = get_ssl_config(
        ssl_enabled=ssl_enabled,
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
        ssl_cert_dir=ssl_cert_dir,
    )

    # Log server startup mode
    if ssl_enabled:
        LOG.info(f"Starting HSYNC server with HTTPS/WSS on port {port}")
        LOG.info(f"Certificate: {ssl_config.get('ssl_certfile', 'N/A')}")
    else:
        LOG.info(f"Starting HSYNC server with HTTP/WS on port {port}")
        LOG.warning("SSL is disabled. Communication is NOT encrypted!")

    uvicorn.run(
        "hsync_server.main:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level,
        reload=True,
        **ssl_config,
    )


if __name__ == "__main__":
    main()

