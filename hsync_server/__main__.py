"""
Entry point for running hsync_server as a module.

Usage:
    python -m hsync_server [options]

This module allows the package to be run directly using Python's -m flag.
"""

import uvicorn

from hsync_server.config import HSYNC_PORT


def main() -> None:
    """Run the HSYNC server using uvicorn."""
    uvicorn.run(
        "hsync_server.main:app",
        host="0.0.0.0",
        port=HSYNC_PORT,
        reload=True,
    )


if __name__ == "__main__":
    main()

