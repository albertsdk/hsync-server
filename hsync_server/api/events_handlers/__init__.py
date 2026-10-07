# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
WebSocket event handlers for HSYNC Server.

This package provides handler functions for different event types:
- handle_added_event: Create new files/directories
- handle_modified_event: Update existing files/directories
- handle_deleted_event: Remove files/directories
- handle_renamed_event: Rename/move files or directories
- handle_symlink_event: Create or update symbolic links
"""
