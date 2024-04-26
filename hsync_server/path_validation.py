# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Path and filename validation utilities for HSYNC Server.

This module provides cross-platform validation for filenames and paths,
ensuring compatibility with Windows, Linux, and macOS file systems.

Based on Microsoft documentation:
https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
"""

# =============================================================================
# Forbidden Characters
# =============================================================================

# Forbidden characters in filenames (cross-platform)
# Windows: < > : " / \ | ? * and characters 0-31
# Linux/macOS: / and NUL (\x00)
# For cross-platform compatibility, we use the union of all restrictions.
FORBIDDEN_FILENAME_CHARS: frozenset[str] = frozenset({
    "<", ">", ":", '"', "/", "\\", "|", "?", "*",
    "\x00",  # NUL - forbidden on all systems
})

# Control characters (ASCII 1-31) - forbidden on Windows
# These are checked separately for efficiency
CONTROL_CHARS: frozenset[str] = frozenset(chr(i) for i in range(1, 32))

# Forbidden characters in path parts (allows / for path separator)
# Same as FORBIDDEN_FILENAME_CHARS but without forward slash
FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset({
    "<", ">", ":", '"', "\\", "|", "?", "*",
    "\x00",  # NUL - forbidden on all systems
})

# =============================================================================
# Reserved Filenames
# =============================================================================

# Reserved filenames (Windows compatibility)
# These names are reserved in every directory and cannot be used as filenames.
# Also reserved when followed by an extension (e.g., NUL.txt, COM1.log)
#
# Note: Windows also recognizes superscript digits ¹²³ (ISO 8859-1) as valid
# parts of COM# and LPT# device names (e.g., COM¹ is reserved).
RESERVED_FILENAMES: frozenset[str] = frozenset({
    # Standard device names
    "CON", "PRN", "AUX", "NUL",
    # Serial ports COM1-COM9 and superscript variants
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "COM\u00b9", "COM\u00b2", "COM\u00b3",  # COM¹, COM², COM³
    # Parallel ports LPT1-LPT9 and superscript variants
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    "LPT\u00b9", "LPT\u00b2", "LPT\u00b3",  # LPT¹, LPT², LPT³
})

# Characters that are problematic at the end of filenames (Windows)
# Windows shell ignores trailing spaces and periods
PROBLEMATIC_TRAILING_CHARS: frozenset[str] = frozenset({" ", "."})


# =============================================================================
# Validation Functions
# =============================================================================


def has_forbidden_chars(name: str, chars: frozenset[str]) -> bool:
    """Check if name contains any forbidden characters."""
    return any(char in name for char in chars)


def has_control_chars(name: str) -> bool:
    """Check if name contains any control characters (ASCII 1-31)."""
    return any(char in name for char in CONTROL_CHARS)


def is_reserved_name(name: str) -> bool:
    """
    Check if name is a Windows reserved filename.

    Handles names with or without extensions (e.g., NUL.txt is also reserved).
    """
    # Get base name without extension for checking reserved names
    base_name = name.split(".")[0].upper() if "." in name else name.upper()
    return base_name in RESERVED_FILENAMES


def has_problematic_trailing(name: str) -> bool:
    """Check if name ends with problematic characters (space or period)."""
    return bool(name) and name[-1] in PROBLEMATIC_TRAILING_CHARS


def is_valid_filename(filename: str) -> bool:
    """
    Validate a filename for cross-platform compatibility.

    Checks for:
    - Empty filename
    - Forbidden characters (cross-platform)
    - Control characters (ASCII 1-31)
    - Reserved names on Windows (including with extensions, e.g., NUL.txt)
    - Problematic trailing characters (space, period)

    Args:
        filename: The filename to validate (without path).

    Returns:
        True if the filename is valid, False otherwise.
    """
    if not filename:
        return False

    if has_forbidden_chars(filename, FORBIDDEN_FILENAME_CHARS):
        return False

    if has_control_chars(filename):
        return False

    if is_reserved_name(filename):
        return False

    if has_problematic_trailing(filename):
        return False

    return True

