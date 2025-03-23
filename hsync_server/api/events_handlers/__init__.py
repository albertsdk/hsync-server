# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket event handlers for HSYNC Server.

This package provides handler functions for different event types:
- handle_added_event: Create new files/directories
- handle_modified_event: Update existing files/directories
- handle_deleted_event: Remove files/directories
- handle_renamed_event: Rename/move files or directories
- handle_symlink_event: Create or update symbolic links
"""

from hsync_common.path_validation import is_safe_symlink_target

from hsync_server.api.events_handlers.handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.api.events_handlers.validation import (
    is_path_within_bucket,
)

__all__ = [
    "is_path_within_bucket",
    "is_safe_symlink_target",
    "handle_added_event",
    "handle_deleted_event",
    "handle_modified_event",
    "handle_renamed_event",
    "handle_symlink_event",
]
