# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
HSYNC Server - HTTP-based file synchronization server.

This package provides a FastAPI-based server for file synchronization
with WebSocket event broadcasting, bucket management, and file operations.
"""

from hsync_server.constants import HSYNC_VERSION

__version__ = HSYNC_VERSION
