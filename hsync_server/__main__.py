"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.
"""

import uvicorn

from hsync_server.config import HSYNC_LOG_LEVEL, HSYNC_PORT
from hsync_server.logger import setup_logging


def main() -> None:
    """Run the HSYNC server using uvicorn."""
    # Configure logging before starting the server
    setup_logging(level=HSYNC_LOG_LEVEL)

    uvicorn.run(
        "hsync_server.main:app",
        host="0.0.0.0",
        port=HSYNC_PORT,
        log_level=HSYNC_LOG_LEVEL,
        reload=True,
    )


if __name__ == "__main__":
    main()

