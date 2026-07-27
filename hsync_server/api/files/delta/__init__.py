# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Delta-transfer (rsync-style) REST endpoints.

Thin FastAPI shells over ``hsync_common.delta``: signature exchange (GET for
upload delta, POST for download delta), delta apply (upload) and the
single-use literal-payload download (token-bound). Wire shapes, status codes
and fallback semantics are identical to the stdlib LAN server handlers.
"""

from hsync_server.api.files.delta.apply import delta_apply
from hsync_server.api.files.delta.data import get_delta_data
from hsync_server.api.files.delta.signatures import (
    get_delta_signatures,
    post_delta_signatures,
)
from hsync_server.api.files.delta.tokens import delta_token_store

__all__ = [
    "delta_apply",
    "delta_token_store",
    "get_delta_data",
    "get_delta_signatures",
    "post_delta_signatures",
]
