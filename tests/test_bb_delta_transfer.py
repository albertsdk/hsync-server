# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the delta-transfer use case.

Drives the real server's delta REST endpoints using only the shared
``hsync_common.delta`` algorithm (the public wire-contract library a real
client uses) — no server internals are imported or mocked.
"""

import hashlib
from pathlib import Path

import httpx
import pytest
import zstandard as zstd
from hsync_common.delta import apply_delta, compute_signatures, optimal_block_size
from hsync_common.models.delta import DeltaOp

pytestmark = pytest.mark.usefixtures("bb_http")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestDeltaDownloadHappy:
    def test_delta_reconstructs_new_file_from_base(
        self, bb_upload, bb_http: httpx.Client, bb_bucket: str, tmp_path: Path
    ):
        # Given a base (v1) the client already has, and a newer version (v2)
        # on the server that differs by a single block. Each base block is
        # distinct (different fill byte) so the matcher produces clean ops.
        v1 = b"".join(bytes([i]) * 4096 for i in range(5))  # 5 distinct 4KB blocks
        v2 = v1[:4096] + bytes([255]) * 4096 + v1[8192:]  # one 4KB block changed
        assert len(v1) == len(v2)
        bb_upload(bb_http, bb_bucket, "delta.bin", v2, sha256sum=_sha(v2))
        base_path = tmp_path / "base.bin"
        base_path.write_bytes(v1)

        # When computing base signatures and requesting a download delta
        block_size = optimal_block_size(len(v1))
        sigs = compute_signatures(str(base_path), block_size)
        match = bb_http.post(
            f"/api/v1/buckets/{bb_bucket}/files/delta/signatures",
            json={
                "path": "delta.bin",
                "block_size": block_size,
                "signatures": [s.to_dict() for s in sigs],
            },
        )
        assert match.status_code == 200
        body = match.json()
        assert body["delta"] is True

        # And fetching the literal payload referenced by the token
        data_url = body["data_url"]
        data_resp = bb_http.get(data_url)
        assert data_resp.status_code == 200
        literals = data_resp.content
        if body.get("compressed"):
            literals = zstd.ZstdDecompressor().decompress(literals)

        # Then applying the ops reconstructs the server's new file exactly
        ops = [
            DeltaOp(
                type=o["type"], offset=o["offset"], length=o["length"], data_offset=o["data_offset"]
            )
            for o in body["instructions"]["ops"]
        ]
        out_path = tmp_path / "rebuilt.bin"
        apply_delta(str(base_path), ops, literals, str(out_path))
        assert out_path.read_bytes() == v2
        assert _sha(out_path.read_bytes()) == body["new_sha256"]


class TestDeltaNegative:
    def test_invalid_token_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a non-existent delta data token
        # When fetching it
        r = bb_http.get(f"/api/v1/buckets/{bb_bucket}/files/delta/data/not-a-real-token")

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_signatures_for_missing_file_returns_404(self, bb_http: httpx.Client, bb_bucket: str):
        # Given a bucket with no such file
        # When requesting its delta signatures
        r = bb_http.get(
            f"/api/v1/buckets/{bb_bucket}/files/delta/signatures", params={"path": "nope.bin"}
        )

        # Then the server reports it as not found
        assert r.status_code == 404

    def test_delta_disabled_returns_404(self, start_live_server, bb_upload):
        # Given a server with delta transfer disabled
        with start_live_server({"HSYNC_DELTA_ENABLED": "false"}) as (base, api_key):
            auth = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(base_url=base, headers=auth, timeout=15.0) as c:
                bucket = "bbnodelta"
                assert c.post("/api/v1/buckets", params={"bucket": bucket}).status_code == 201
                assert bb_upload(c, bucket, "f.bin", b"x" * 100).status_code == 201

                # When requesting delta signatures
                r = c.get(
                    f"/api/v1/buckets/{bucket}/files/delta/signatures", params={"path": "f.bin"}
                )

                # Then the endpoint reports not found with the delta-disabled
                # detail (not a missing-file 404)
                assert r.status_code == 404
                assert r.json().get("detail") == "delta_disabled"
