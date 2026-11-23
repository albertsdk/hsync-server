# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box test for the authenticated Prometheus ``/metrics`` endpoint.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os

import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


class TestMetricsEndpoint:
    def test_authenticated_metrics_are_prometheus_text(self, bb_http):
        # Given an authenticated admin client
        # When it reads the monitoring endpoint
        r = bb_http.get("/metrics")

        # Then the endpoint answers with a Prometheus exposition document
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/plain")
        assert "# HELP" in r.text
        # …and never leaks the bearer credential it was authenticated with.
        assert "Bearer" not in r.text
