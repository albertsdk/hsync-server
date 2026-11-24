# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the admin history surface (ADR-0057).

Drives the real server over HTTP only, as an operator without a shell would: a
file is overwritten and deleted through the public endpoints, its preserved
versions are listed on the admin route and one of them is restored. Everything
asserted here is observable on the wire — the record shape, the status each
restore answers and the bytes that come back afterwards.
"""

import os

import httpx
import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]

_CID = "bbclient0001bbcltBB10"


def _vclock(counter: int) -> str:
    """A causal clock for *_CID*: the server resolves a write by it, not by time."""
    import json

    return json.dumps({_CID: counter})


def _history(client: httpx.Client, bucket: str) -> dict:
    response = client.get(f"/api/v1/admin/buckets/{bucket}/history")
    assert response.status_code == 200, response.text
    return response.json()


def _restore(client: httpx.Client, bucket: str, entry_id: str, *, overwrite: bool = False):
    url = f"/api/v1/admin/buckets/{bucket}/history/{entry_id}/restore"
    return client.post(url, json={"overwrite": True}) if overwrite else client.post(url)


def _download(client: httpx.Client, bucket: str, filename: str) -> bytes:
    response = client.get(
        f"/api/v1/buckets/{bucket}/files/download",
        params={"filename": filename, "cid": _CID},
    )
    assert response.status_code == 200, response.text
    return response.content


class TestAdminHistory:
    def test_an_overwritten_version_is_listed_and_restorable(self, bb_http, bb_upload, bb_bucket):
        # Given a file that is uploaded and then replaced by a causally newer write
        bb_upload(bb_http, bb_bucket, "note.txt", b"first draft", vclock=_vclock(1))
        replaced = bb_upload(bb_http, bb_bucket, "note.txt", b"second draft", vclock=_vclock(2))
        assert replaced.status_code == 201, replaced.text
        assert _download(bb_http, bb_bucket, "note.txt") == b"second draft"

        # Then the replaced bytes are listed, without any server path
        body = _history(bb_http, bb_bucket)
        assert body["bucket"] == bb_bucket
        (entry,) = [item for item in body["entries"] if item["path"] == "note.txt"]
        assert entry["size"] == len(b"first draft")
        assert entry["reason"] == "overwrite"
        assert entry["recorded_at"] > 0

        # When asking to restore while the path is occupied
        refused = _restore(bb_http, bb_bucket, entry["entry_id"])
        # Then the current content is untouched
        assert refused.status_code == 409
        assert _download(bb_http, bb_bucket, "note.txt") == b"second draft"

        # When allowing the overwrite
        restored = _restore(bb_http, bb_bucket, entry["entry_id"], overwrite=True)
        # Then the preserved bytes are live and the replaced ones are kept in turn
        assert restored.status_code == 200
        assert restored.json()["status"] == "restored"
        assert _download(bb_http, bb_bucket, "note.txt") == b"first draft"
        assert len(_history(bb_http, bb_bucket)["entries"]) == 2

    def test_a_deleted_file_is_restored(self, bb_http, bb_upload, bb_bucket):
        # Given an uploaded file that is then deleted
        bb_upload(bb_http, bb_bucket, "gone.txt", b"keep me")
        deleted = bb_http.request(
            "DELETE",
            f"/api/v1/buckets/{bb_bucket}/files",
            data={"cid": _CID, "filename": "gone.txt"},
        )
        assert deleted.status_code == 200, deleted.text

        # Then the deletion is preserved as its own reason
        (entry,) = [
            item for item in _history(bb_http, bb_bucket)["entries"] if item["path"] == "gone.txt"
        ]
        assert entry["reason"] == "delete"

        # When restoring it, the path is readable again
        assert _restore(bb_http, bb_bucket, entry["entry_id"]).status_code == 200
        assert _download(bb_http, bb_bucket, "gone.txt") == b"keep me"

    def test_an_unknown_entry_is_not_found(self, bb_http, bb_bucket):
        response = _restore(bb_http, bb_bucket, "1700000000-deadbeef")

        assert response.status_code == 404
        assert response.json()["error"] == "HSYNC-2000"

    def test_a_client_token_cannot_read_the_history(self, bb_http, bb_bucket):
        # Given a client account created by the admin (its token is returned once)
        created = bb_http.post(
            "/api/v1/admin/clients",
            json={"name": f"hist-{bb_bucket}", "role": "client"},
        )
        assert created.status_code == 201, created.text
        account = created.json()

        # Then the admin route refuses the client role
        response = bb_http.get(
            f"/api/v1/admin/buckets/{bb_bucket}/history",
            headers={"Authorization": f"Bearer {account['token']}"},
        )
        assert response.status_code == 403

        # Cleanup
        assert bb_http.delete(f"/api/v1/admin/clients/{account['id']}").status_code == 200
