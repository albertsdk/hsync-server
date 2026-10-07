# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Contract drift check for the committed REST OpenAPI snapshot.

The committed snapshot in ``hsync_common.protocol`` is the single source of
truth for the REST surface shared with the Python and Android clients. This
test re-derives the schema from the live FastAPI app and fails on any
divergence, preventing silent contract drift.
"""

from hsync_common.protocol import REST_OPENAPI_PATH, dump_openapi

from hsync_server.main import app


class TestOpenApiContract:
    """Guard the committed OpenAPI snapshot against silent drift."""

    def test_snapshot_file_exists(self):
        assert REST_OPENAPI_PATH.is_file(), (
            f"Missing OpenAPI snapshot at {REST_OPENAPI_PATH}. "
            "Generate it with: poetry run python scripts/export_openapi.py"
        )

    def test_snapshot_matches_live_schema(self):
        current = dump_openapi(app.openapi())
        committed = REST_OPENAPI_PATH.read_text(encoding="utf-8")
        assert current == committed, (
            "OpenAPI contract drift detected: the live FastAPI schema differs "
            "from the committed snapshot. Regenerate it with:\n"
            "  poetry run python scripts/export_openapi.py"
        )
