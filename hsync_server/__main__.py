"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.

Environment Variables:
    HSYNC_PORT: Server port (default: 8765)
    HSYNC_LOG_LEVEL: Log level (default: info)
    HSYNC_SSL_ENABLED: Enable HTTPS/WSS (default: false)
    HSYNC_SSL_CERTFILE: Path to SSL certificate (optional)
    HSYNC_SSL_KEYFILE: Path to SSL private key (optional)
    HSYNC_SSL_CERT_DIR: Directory for auto-generated certs (default: ./certs)
"""

import uvicorn

from hsync_server.config import (
    HSYNC_LOG_LEVEL,
    HSYNC_PORT,
    HSYNC_SSL_ENABLED,
    HSYNC_SSL_CERTFILE,
    HSYNC_SSL_KEYFILE,
    HSYNC_SSL_CERT_DIR,
)
from hsync_server.logger import setup_logging
from hsync_server.ssl_utils import get_ssl_config


def main() -> None:
    """Run the HSYNC server using uvicorn."""
    # Configure logging before starting the server
    LOG = setup_logging(level=HSYNC_LOG_LEVEL)

    # Get SSL configuration (empty dict if SSL disabled)
    ssl_config = get_ssl_config(
        ssl_enabled=HSYNC_SSL_ENABLED,
        ssl_certfile=HSYNC_SSL_CERTFILE or None,
        ssl_keyfile=HSYNC_SSL_KEYFILE or None,
        ssl_cert_dir=HSYNC_SSL_CERT_DIR,
    )

    # Log server startup mode
    if HSYNC_SSL_ENABLED:
        LOG.info(f"Starting HSYNC server with HTTPS/WSS on port {HSYNC_PORT}")
        LOG.info(f"Certificate: {ssl_config.get('ssl_certfile', 'N/A')}")
    else:
        LOG.info(f"Starting HSYNC server with HTTP/WS on port {HSYNC_PORT}")
        LOG.warning("SSL is disabled. Communication is NOT encrypted!")

    uvicorn.run(
        "hsync_server.main:app",
        host="0.0.0.0",
        port=HSYNC_PORT,
        log_level=HSYNC_LOG_LEVEL,
        reload=True,
        **ssl_config,
    )


if __name__ == "__main__":
    main()

