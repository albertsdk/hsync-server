# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File operations API subpackage.

This module provides REST API endpoints for file management:
- File upload with integrity verification (SHA-256)
- File download with streaming response
- File listing with sorting options
- File deletion with event broadcasting
"""

from hsync_server.api.files.delete import delete_file
from hsync_server.api.files.delta import (
    delta_apply,
    get_delta_data,
    get_delta_signatures,
    post_delta_signatures,
)
from hsync_server.api.files.download import download_file
from hsync_server.api.files.list import (
    get_dirs,
    get_files,
    get_tombstones,
    get_upload_status,
    post_tombstones,
)
from hsync_server.api.files.permissions import (
    apply_permissions_to_existing_file,
    patch_file_permissions,
)
from hsync_server.api.files.symlinks import create_symlink
from hsync_server.api.files.upload import upload_file

__all__ = [
    "upload_file",
    "download_file",
    "get_dirs",
    "get_files",
    "get_tombstones",
    "post_tombstones",
    "get_upload_status",
    "delete_file",
    "create_symlink",
    "patch_file_permissions",
    "apply_permissions_to_existing_file",
    "delta_apply",
    "get_delta_data",
    "get_delta_signatures",
    "post_delta_signatures",
]
