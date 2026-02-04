"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

Options:
    --env-file PATH  Load environment variables from file before starting

This module allows the package to be run directly using Python's -m flag.

Environment Variables:
    HSYNC_PORT: Server port (default: 8765)
    HSYNC_LOG_LEVEL: Log level (default: info)
    HSYNC_SSL_ENABLED: Enable HTTPS/WSS (default: false)
    HSYNC_SSL_CERTFILE: Path to SSL certificate (optional)
    HSYNC_SSL_KEYFILE: Path to SSL private key (optional)
    HSYNC_SSL_CERT_DIR: Directory for auto-generated certs (default: ./certs)
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
from hsync_server.logger import setup_logging
from hsync_server.ssl_utils import get_ssl_config


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        prog="hsync-server",
        description="HSYNC Server - HTTP-based file synchronization server",
    )

    parser.add_argument(
        "--env-file",
        type=str,
        required=False,
        metavar="PATH",
        dest="env_file",
        help="Load environment variables from file before starting.",
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

    # Get configuration values (after env file is loaded)
    log_level = get_log_level()
    port = get_port()
    ssl_enabled = get_ssl_enabled()
    ssl_certfile = get_ssl_certfile()
    ssl_keyfile = get_ssl_keyfile()
    ssl_cert_dir = get_ssl_cert_dir()

    # Configure logging before starting the server
    LOG = setup_logging(level=log_level)

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

