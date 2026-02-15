# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File operations API subpackage.

This module provides REST API endpoints for file management:
- File upload with integrity verification (MD5)
- File download with streaming response
- File listing with sorting options
- File deletion with event broadcasting
"""

from hsync_server.api.files.upload import upload_file
from hsync_server.api.files.download import download_file
from hsync_server.api.files.list import get_files, get_upload_status
from hsync_server.api.files.delete import delete_file

__all__ = [
    "upload_file",
    "download_file",
    "get_files",
    "get_upload_status",
    "delete_file",
]
