# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso
"""Black-box **security probes** for hsync-server.

Adversarial suite: each test is an attack attempt against the public surface
(HTTP/WS), never against the implementation — the same probes must keep
passing against any conforming deployment. Attack classes covered:

* **Authentication** — no-auth, malformed/tampered token, wrong scheme.
* **Path traversal / confinement** — upload/download/delete/list/permissions
  traversal, filename traversal, symlink escape, cross-bucket access, and the
  ``/dirs`` ``path``-param handling.
* **Upload integrity / DoS** — checksum mismatch, decompression bomb, oversize
  declared, delta-apply oversize, delta-apply base mismatch.
* **Delta token** — single-use, cross-bucket redeem (must be rejected).
* **Info disclosure** — ``/health?size=`` amplification, ``/metrics``,
  ``/docs``/``/redoc``/``/openapi.json``, account-name enumeration, bucket quota.
* **WebSocket** — bad token, invalid cid, traversal/symlink-escape event
  dropped, log-injection robustness.

Opt-in: requires ``HSYNC_BB_SECURITY=1`` **on top of** the suite's
``HSYNC_BB_SERVER=1`` (security probes are adversarial by nature — they send
deliberately hostile traffic, so they stay out of the default black-box run).
Scale knobs, read once at import:

* ``HSYNC_BB_SERVER_SECURITY_QUOTA_BUCKETS`` (default 110) — max buckets the
  no-quota probe attempts (must exceed the server's per-account quota to detect
  it).

The shared probes live in ``pentest/probes.py``; the standalone runner is
``pentest/run_pentest.py``; the findings report is ``pentest/SECURITY_FINDINGS.md``.
"""

import os

import pytest

from pentest.probes import Pentest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_ENABLE_ENV = "HSYNC_BB_SECURITY"
_SECURITY_ENABLED = os.environ.get(_ENABLE_ENV) == "1"
_REASON = (
    "server security probes are opt-in; set HSYNC_BB_SECURITY=1 (and HSYNC_BB_SERVER=1) to enable"
)

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED or not _SECURITY_ENABLED, reason=_REASON)


def _knob(name: str, default: int) -> int:
    """Positive-int environment knob (import time; tests scrub HSYNC_*)."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


_QUOTA_BUCKETS = _knob("HSYNC_BB_SERVER_SECURITY_QUOTA_BUCKETS", 110)


@pytest.fixture(scope="module")
def pt(live_server: tuple[str, str]) -> Pentest:
    base_url, api_key = live_server
    return Pentest(base_url, api_key)


def _assert_secure(result):
    assert result.status == "SECURE", f"{result.id}: {result.detail}"


# =============================================================================
# Authentication
# =============================================================================
class TestAuth:
    def test_no_auth(self, pt):
        _assert_secure(pt.probe_no_auth())

    def test_bad_token(self, pt):
        _assert_secure(pt.probe_bad_token())

    def test_token_tamper(self, pt):
        _assert_secure(pt.probe_token_tamper())

    def test_wrong_scheme(self, pt):
        _assert_secure(pt.probe_wrong_scheme())


# =============================================================================
# Path traversal / confinement
# =============================================================================
class TestPathTraversal:
    def test_upload_path(self, pt):
        _assert_secure(pt.probe_upload_path_traversal())

    def test_upload_filename(self, pt):
        _assert_secure(pt.probe_upload_filename_traversal())

    def test_download(self, pt):
        _assert_secure(pt.probe_download_traversal())

    def test_delete(self, pt):
        _assert_secure(pt.probe_delete_traversal())

    def test_list(self, pt):
        _assert_secure(pt.probe_list_traversal())

    def test_dirs_no_cross_bucket_leak(self, pt):
        _assert_secure(pt.probe_dirs_no_cross_bucket_leak())

    def test_permissions(self, pt):
        _assert_secure(pt.probe_permissions_traversal())

    def test_symlink_escape(self, pt):
        _assert_secure(pt.probe_symlink_escape())

    def test_symlink_safe_control(self, pt):
        _assert_secure(pt.probe_symlink_safe_allowed())

    def test_cross_bucket_download(self, pt):
        _assert_secure(pt.probe_cross_bucket_download())


# =============================================================================
# Upload integrity / DoS
# =============================================================================
class TestUploadIntegrity:
    def test_checksum_mismatch(self, pt):
        _assert_secure(pt.probe_upload_checksum_mismatch())

    def test_decompression_bomb(self, pt):
        _assert_secure(pt.probe_upload_decompression_bomb())

    def test_oversize_declared(self, pt):
        _assert_secure(pt.probe_upload_oversize_declared())

    def test_delta_apply_oversize(self, pt):
        _assert_secure(pt.probe_delta_apply_oversize())

    def test_delta_apply_base_mismatch(self, pt):
        _assert_secure(pt.probe_delta_apply_base_mismatch())


# =============================================================================
# Delta token
# =============================================================================
class TestDeltaToken:
    def test_single_use(self, pt):
        _assert_secure(pt.probe_delta_token_single_use())

    def test_cross_bucket_rejected(self, pt):
        # The server binds the delta token to its bucket (data.py: entry.bucket
        # != bucket -> 404), so a token minted for A cannot be redeemed via B.
        _assert_secure(pt.probe_delta_token_cross_bucket())


# =============================================================================
# Info disclosure
# =============================================================================
class TestInfoDisclosure:
    def test_health_amplification(self, pt):
        # Fixed: /health?size= cap reduced to 256 KB (client autotune default) — F-01.
        _assert_secure(pt.probe_health_amplification())

    def test_health_size_bounded(self, pt):
        _assert_secure(pt.probe_health_size_bounded())

    def test_metrics_unauth(self, pt):
        # Fixed: /metrics now requires authentication (metrics.py auth_router route).
        _assert_secure(pt.probe_metrics_unauthenticated())

    def test_docs_ui_disabled(self, pt):
        _assert_secure(pt.probe_docs_ui_disabled())

    def test_openapi_exposed(self, pt):
        # Fixed: main.py now sets openapi_url=None when enable_docs=false.
        _assert_secure(pt.probe_openapi_exposed())

    def test_account_enumeration(self, pt):
        # Fixed: grant_member returns a uniform 201 (no existence oracle) — F-03.
        _assert_secure(pt.probe_account_enumeration())

    def test_bucket_quota(self, pt):
        # Fixed: create_bucket enforces a per-account quota (F-04).
        _assert_secure(pt.probe_bucket_quota(count=_QUOTA_BUCKETS))


# =============================================================================
# WebSocket
# =============================================================================
class TestWebSocket:
    async def test_no_auth(self, pt):
        _assert_secure(await pt.probe_ws_no_auth())

    async def test_invalid_cid(self, pt):
        _assert_secure(await pt.probe_ws_invalid_cid())

    async def test_event_traversal_dropped(self, pt):
        _assert_secure(await pt.probe_ws_event_traversal_dropped())

    async def test_symlink_escape_dropped(self, pt):
        _assert_secure(await pt.probe_ws_symlink_escape_dropped())

    async def test_log_injection_no_crash(self, pt):
        _assert_secure(await pt.probe_ws_log_injection_no_crash())
