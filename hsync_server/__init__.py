# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
HSYNC Server - HTTP-based file synchronization server.

This package provides a FastAPI-based server for file synchronization
with WebSocket event broadcasting, bucket management, and file operations.
"""

from hsync_server.constants import HSYNC_VERSION

__version__ = HSYNC_VERSION
