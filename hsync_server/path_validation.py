# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Backward-compatibility shim for path validation.

The implementation now lives in :mod:`hsync_common.path_validation` and is
re-exported here so existing ``from hsync_server.path_validation import ...``
call sites keep working.
"""

from hsync_common.path_validation import (
    CONTROL_CHARS,
    FORBIDDEN_FILENAME_CHARS,
    FORBIDDEN_PATH_CHARS,
    PROBLEMATIC_TRAILING_CHARS,
    RESERVED_FILENAMES,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
    is_safe_path_component,
    is_valid_filename,
    is_valid_path,
    sanitize_filename,
)

__all__ = [
    "CONTROL_CHARS",
    "FORBIDDEN_FILENAME_CHARS",
    "FORBIDDEN_PATH_CHARS",
    "PROBLEMATIC_TRAILING_CHARS",
    "RESERVED_FILENAMES",
    "has_control_chars",
    "has_forbidden_chars",
    "has_problematic_trailing",
    "is_reserved_name",
    "is_safe_path_component",
    "is_valid_filename",
    "is_valid_path",
    "sanitize_filename",
]
