# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared helpers for the delta REST endpoints.

Single source for the disabled-feature response: the canonical payload is
``{"detail": "delta_disabled"}`` (pinned by the black-box test suite), so the
signature-exchange, delta-data and delta-apply endpoints can never drift apart.
"""

from fastapi.responses import JSONResponse


def delta_disabled_response() -> JSONResponse:
    """404 response returned when the delta feature is disabled."""
    return JSONResponse(status_code=404, content={"detail": "delta_disabled"})
