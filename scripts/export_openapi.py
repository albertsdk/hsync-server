#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Export the server's OpenAPI schema to the shared protocol contract.

This is the only supported way to update the committed REST snapshot
(``hsync_common.protocol.REST_OPENAPI_PATH``). The matching drift-check test
(`tests/test_openapi_contract.py`) fails whenever the live FastAPI schema
diverges from the committed file, so run this script after any API change.

Usage:
    poetry run python scripts/export_openapi.py
"""

from __future__ import annotations

from hsync_common.protocol import REST_OPENAPI_PATH, dump_openapi

from hsync_server.main import app


def main() -> None:
    """Regenerate and write the canonical OpenAPI snapshot."""
    REST_OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    REST_OPENAPI_PATH.write_text(dump_openapi(app.openapi()), encoding="utf-8")
    print(f"Wrote {REST_OPENAPI_PATH}")


if __name__ == "__main__":
    main()
