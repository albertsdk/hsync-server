"""
File model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.file.
"""

from hsync_common.models.file import (  # noqa: F401
    File,
    FileDict,
    clear_sha256_cache,
    invalidate_sha256_cache,
    is_hidden_file,
)
