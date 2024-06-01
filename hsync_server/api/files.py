# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File operations API for HSYNC Server.

This module provides REST API endpoints for file management:
- File upload with integrity verification (MD5)
- File download with streaming response
- File listing with sorting options
- File deletion with event broadcasting

Note: This module is now a compatibility layer. The actual implementation
is in the hsync_server.api.files subpackage.

All endpoints require API key authentication via Bearer token.
Rate limiting is applied to protect against abuse.
"""

from loguru import logger as log

from hsync_server.api.files.delete import delete_file  # noqa: F401
from hsync_server.api.files.download import download_file  # noqa: F401
from hsync_server.api.files.list import get_files, get_upload_status  # noqa: F401

# Re-export from submodules for backward compatibility
from hsync_server.api.files.upload import upload_file  # noqa: F401

log.info(f"Module {__name__} loaded (compatibility layer)")
