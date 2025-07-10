# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Integration tests for the delta transfer REST endpoints."""

from __future__ import annotations

import json
import random
from hashlib import sha256
from pathlib import Path

import pytest
from hsync_common.delta import (
    apply_delta,
    compute_signatures,
    find_matches,
    optimal_block_size,
)
from hsync_common.models.delta import BlockSignature, DeltaOp

from hsync_server.api.files.delta.tokens import delta_token_store

pytestmark = pytest.mark.anyio

BLOCK = 4096  # DELTA_BLOCK_SIZE_SMALL: files < 1 MB


@pytest.fixture(autouse=True)
def _reset_delta_state():
    """Isolate the global token store and signature cache between tests."""
    from hsync_server.api.files.delta import signatures as sig_module

    delta_token_store._tokens.clear()
    sig_module._signature_cache.clear()
    yield
    for entry in list(delta_token_store._tokens.values()):
        delta_token_store._delete_payload(entry)
    delta_token_store._tokens.clear()
    sig_module._signature_cache.clear()


@pytest.fixture
def bucket_dir(temp_buckets_dir: str) -> Path:
    """A bucket with one sizeable file, ready for delta operations."""
    d = Path(temp_buckets_dir) / "bucket1"
    d.mkdir(parents=True)
    return d


def _base_bytes(seed: int = 7, size: int = BLOCK * 12) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


def _write_base(bucket_dir: Path, data: bytes, rel: str = "docs/big.bin") -> str:
    target = bucket_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


# ---------------------------------------------------------------------------
# GET /delta/signatures (upload delta)
# ---------------------------------------------------------------------------


class TestGetSignatures:
    async def test_returns_signatures(self, client, bucket_dir):
        base = _base_bytes()
        rel = _write_base(bucket_dir, base)
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": rel}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["block_size"] == optimal_block_size(len(base))
        assert body["file_size"] == len(base)
        assert body["sha256"] == sha256(base).hexdigest()
        assert len(body["signatures"]) == 12
        first = body["signatures"][0]
        assert first["index"] == 0 and first["offset"] == 0 and first["length"] == BLOCK
        assert first["strong"] == sha256(base[:BLOCK]).hexdigest()

    async def test_missing_file_is_404(self, client, bucket_dir):
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": "nope.bin"}
        )
        assert resp.status_code == 404

    async def test_path_traversal_rejected(self, client, bucket_dir):
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": "../escape"}
        )
        assert resp.status_code == 400

    async def test_disabled_feature_is_404(self, client, bucket_dir, monkeypatch):
        monkeypatch.setattr(
            "hsync_server.api.files.delta.signatures.get_delta_enabled", lambda: False
        )
        rel = _write_base(bucket_dir, _base_bytes())
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": rel}
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /delta/signatures (download delta)
# ---------------------------------------------------------------------------


def _client_sigs(tmp_path: Path, base: bytes, block_size: int) -> list[dict]:
    base_path = tmp_path / "client-base.bin"
    base_path.write_bytes(base)
    return [s.to_dict() for s in compute_signatures(str(base_path), block_size)]


class TestPostSignatures:
    async def test_delta_true_with_instructions(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = bytearray(base)
        new[100:164] = b"Z" * 64  # localized change inside block 0
        rel = _write_base(bucket_dir, bytes(new))
        sigs = _client_sigs(tmp_path, base, BLOCK)
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={"path": rel, "block_size": BLOCK, "signatures": sigs},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["delta"] is True
        assert body["new_sha256"] == sha256(bytes(new)).hexdigest()
        assert body["instructions"]["new_size"] == len(new)
        assert body["data_url"].startswith("/api/v1/buckets/bucket1/files/delta/data/")
        ops = body["instructions"]["ops"]
        assert any(op["type"] == "copy" for op in ops)

    async def test_delta_false_when_mostly_changed(self, client, bucket_dir, tmp_path):
        base = _base_bytes(seed=1)
        new = _base_bytes(seed=2)  # completely different content
        rel = _write_base(bucket_dir, new)
        sigs = _client_sigs(tmp_path, base, BLOCK)
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={"path": rel, "block_size": BLOCK, "signatures": sigs},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["delta"] is False
        assert body["reason"] == "too_many_changed_blocks"

    async def test_missing_file_is_404(self, client, bucket_dir, tmp_path):
        sigs = _client_sigs(tmp_path, _base_bytes(), BLOCK)
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={"path": "ghost.bin", "block_size": BLOCK, "signatures": sigs},
        )
        assert resp.status_code == 404

    async def test_invalid_block_size_is_400(self, client, bucket_dir, tmp_path):
        rel = _write_base(bucket_dir, _base_bytes())
        sigs = _client_sigs(tmp_path, _base_bytes(), BLOCK)
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={"path": rel, "block_size": 12345, "signatures": sigs},
        )
        assert resp.status_code == 400

    async def test_malformed_signatures_is_400(self, client, bucket_dir):
        rel = _write_base(bucket_dir, _base_bytes())
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={
                "path": rel,
                "block_size": BLOCK,
                "signatures": [{"index": -1, "offset": 0, "length": 1, "weak": 1, "strong": "x"}],
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /delta/data/{token}
# ---------------------------------------------------------------------------


async def _prepare_delta(client, bucket_dir, tmp_path, base: bytes, new: bytes) -> dict:
    rel = _write_base(bucket_dir, new)
    sigs = _client_sigs(tmp_path, base, BLOCK)
    resp = await client.post(
        "/api/v1/buckets/bucket1/files/delta/signatures",
        json={"path": rel, "block_size": BLOCK, "signatures": sigs},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["delta"] is True
    return body


class TestGetDeltaData:
    async def test_download_payload(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = b"HEAD" + base
        body = await _prepare_delta(client, bucket_dir, tmp_path, base, new)
        resp = await client.get(body["data_url"])
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/octet-stream"
        assert len(resp.content) == body["data_size"]

    async def test_unknown_token_is_404(self, client):
        resp = await client.get("/api/v1/buckets/bucket1/files/delta/data/deadbeef")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "token_not_found"

    async def test_expired_token_is_410(self, client, bucket_dir, tmp_path, monkeypatch):
        base = _base_bytes()
        new = b"HEAD" + base
        body = await _prepare_delta(client, bucket_dir, tmp_path, base, new)
        token = body["data_url"].rsplit("/", 1)[-1]
        entry = delta_token_store.lookup(token)
        monkeypatch.setattr(
            "hsync_server.api.files.delta.data.delta_token_store.is_expired",
            lambda e: True,
        )
        resp = await client.get(body["data_url"])
        assert resp.status_code == 410
        assert resp.json()["detail"] == "token_expired"

    async def test_wrong_bucket_binding_is_404(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = b"HEAD" + base
        body = await _prepare_delta(client, bucket_dir, tmp_path, base, new)
        other_url = body["data_url"].replace("/buckets/bucket1/", "/buckets/bucket2/")
        resp = await client.get(other_url)
        assert resp.status_code == 404

    async def test_token_is_single_use(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = b"HEAD" + base
        body = await _prepare_delta(client, bucket_dir, tmp_path, base, new)
        first = await client.get(body["data_url"])
        assert first.status_code == 200
        second = await client.get(body["data_url"])
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# POST /delta/apply (upload delta)
# ---------------------------------------------------------------------------


def _apply_multipart(
    base: bytes, new: bytes, tmp_path: Path, block_size: int = BLOCK, **meta_overrides
) -> dict:
    """Simulate the client side: compute ops/literals and build the multipart body."""
    base_path = tmp_path / "apply-base.bin"
    base_path.write_bytes(base)
    sigs = compute_signatures(str(base_path), block_size)
    ops, literals = find_matches(new, sigs, block_size)
    metadata = {
        "cid": "testclient",
        "base_sha256": sha256(base).hexdigest(),
        "new_sha256": sha256(new).hexdigest(),
        "new_size": len(new),
        "block_size": block_size,
        "compressed": False,
        "ops": [op.to_dict() for op in ops],
    }
    metadata.update(meta_overrides)
    return {
        "data": {"path": "docs/big.bin", "metadata": json.dumps(metadata)},
        "files": {"file": ("delta.bin", literals, "application/octet-stream")},
    }


class TestDeltaApply:
    async def test_successful_apply(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = bytearray(base)
        new[BLOCK : BLOCK + 32] = b"Q" * 32
        new += b"tail-growth"
        _write_base(bucket_dir, base)
        payload = _apply_multipart(base, bytes(new), tmp_path)
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["sha256"] == sha256(bytes(new)).hexdigest()
        assert body["size"] == len(new)
        assert (bucket_dir / "docs/big.bin").read_bytes() == bytes(new)

    async def test_base_changed_is_409(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = b"changed" + base
        _write_base(bucket_dir, base)
        payload = _apply_multipart(base, new, tmp_path)
        # Someone else modifies the file before the apply lands.
        (bucket_dir / "docs/big.bin").write_bytes(_base_bytes(seed=99))
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "base_file_changed"

    async def test_missing_file_is_404(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        payload = _apply_multipart(base, base + b"x", tmp_path)
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 404

    async def test_ops_out_of_bounds_is_422(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = base + b"extra"
        _write_base(bucket_dir, base)
        payload = _apply_multipart(base, new, tmp_path)
        meta = json.loads(payload["data"]["metadata"])
        meta["ops"] = [{"type": "copy", "offset": len(base) + 5000, "length": 10, "data_offset": 0}]
        meta["new_size"] = 10
        payload["data"]["metadata"] = json.dumps(meta)
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "copy_op_out_of_bounds"

    async def test_coverage_mismatch_is_422(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = base + b"extra"
        _write_base(bucket_dir, base)
        payload = _apply_multipart(base, new, tmp_path)
        meta = json.loads(payload["data"]["metadata"])
        meta["new_size"] = 12345  # does not match the ops' total length
        payload["data"]["metadata"] = json.dumps(meta)
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ops_coverage_mismatch"

    async def test_hash_mismatch_is_422(self, client, bucket_dir, tmp_path):
        base = _base_bytes()
        new = base + b"extra"
        _write_base(bucket_dir, base)
        payload = _apply_multipart(base, new, tmp_path)
        meta = json.loads(payload["data"]["metadata"])
        meta["new_sha256"] = sha256(b"different-content").hexdigest()
        payload["data"]["metadata"] = json.dumps(meta)
        resp = await client.post("/api/v1/buckets/bucket1/files/delta/apply", **payload)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "reconstructed_file_hash_mismatch"
        # The on-disk file must be untouched.
        assert (bucket_dir / "docs/big.bin").read_bytes() == base

    async def test_invalid_metadata_is_400(self, client, bucket_dir):
        _write_base(bucket_dir, _base_bytes())
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/apply",
            data={"path": "docs/big.bin", "metadata": "not-json"},
            files={"file": ("delta.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Full round-trips through the wire
# ---------------------------------------------------------------------------


class TestRoundTrips:
    async def test_upload_delta_roundtrip(self, client, bucket_dir, tmp_path):
        """GET signatures → client computes delta → POST apply → file updated."""
        base = _base_bytes()
        rel = _write_base(bucket_dir, base)
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": rel}
        )
        assert resp.status_code == 200
        sig_body = resp.json()
        assert sig_body["sha256"] == sha256(base).hexdigest()  # early-out check passes

        new = bytearray(base)
        new[50:110] = b"M" * 60
        new.extend(b"GROW")
        sigs = [BlockSignature(**s) for s in sig_body["signatures"]]
        ops, literals = find_matches(bytes(new), sigs, sig_body["block_size"])
        metadata = {
            "cid": "testclient",
            "base_sha256": sig_body["sha256"],
            "new_sha256": sha256(bytes(new)).hexdigest(),
            "new_size": len(new),
            "block_size": sig_body["block_size"],
            "compressed": False,
            "ops": [op.to_dict() for op in ops],
        }
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/apply",
            data={"path": rel, "metadata": json.dumps(metadata)},
            files={"file": ("delta.bin", literals, "application/octet-stream")},
        )
        assert resp.status_code == 200
        assert (bucket_dir / rel).read_bytes() == bytes(new)
        # The literal payload is far smaller than the whole file.
        assert len(literals) < len(new) // 4

    async def test_download_delta_roundtrip(self, client, bucket_dir, tmp_path):
        """POST signatures → GET data → client rebuilds byte-identical file."""
        base = _base_bytes()
        new = bytearray(base)
        new[BLOCK * 2 : BLOCK * 2 + 128] = b"D" * 128
        rel = _write_base(bucket_dir, bytes(new))

        client_base = tmp_path / "local-base.bin"
        client_base.write_bytes(base)
        sigs = [s.to_dict() for s in compute_signatures(str(client_base), BLOCK)]
        resp = await client.post(
            "/api/v1/buckets/bucket1/files/delta/signatures",
            json={"path": rel, "block_size": BLOCK, "signatures": sigs},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["delta"] is True

        payload_resp = await client.get(body["data_url"])
        assert payload_resp.status_code == 200

        ops = [
            DeltaOp(
                type=o["type"],
                offset=o["offset"],
                length=o["length"],
                data_offset=o["data_offset"],
            )
            for o in body["instructions"]["ops"]
        ]
        out = tmp_path / "rebuilt.bin"
        apply_delta(str(client_base), ops, payload_resp.content, str(out))
        assert out.read_bytes() == bytes(new)
        assert sha256(out.read_bytes()).hexdigest() == body["new_sha256"]


# ---------------------------------------------------------------------------
# Concurrency: per-file lock serializes concurrent applies
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_applies_serialized(self, client, bucket_dir):
        """Two concurrent delta applies on the same file must be serialized."""
        import asyncio

        base = _base_bytes()
        rel = _write_base(bucket_dir, base)

        # Get signatures once (both applies will use the same base)
        resp = await client.get(
            "/api/v1/buckets/bucket1/files/delta/signatures", params={"path": rel}
        )
        assert resp.status_code == 200
        sig_body = resp.json()

        # Create two different modifications
        new1 = bytearray(base)
        new1[50:110] = b"A" * 60
        sigs = [BlockSignature(**s) for s in sig_body["signatures"]]
        ops1, literals1 = find_matches(bytes(new1), sigs, sig_body["block_size"])
        metadata1 = {
            "cid": "client1",
            "base_sha256": sig_body["sha256"],
            "new_sha256": sha256(bytes(new1)).hexdigest(),
            "new_size": len(new1),
            "block_size": sig_body["block_size"],
            "compressed": False,
            "ops": [op.to_dict() for op in ops1],
        }

        new2 = bytearray(base)
        new2[50:110] = b"B" * 60
        ops2, literals2 = find_matches(bytes(new2), sigs, sig_body["block_size"])
        metadata2 = {
            "cid": "client2",
            "base_sha256": sig_body["sha256"],
            "new_sha256": sha256(bytes(new2)).hexdigest(),
            "new_size": len(new2),
            "block_size": sig_body["block_size"],
            "compressed": False,
            "ops": [op.to_dict() for op in ops2],
        }

        # Launch both applies concurrently
        async def apply_delta_req(metadata, literals):
            return await client.post(
                "/api/v1/buckets/bucket1/files/delta/apply",
                data={"path": rel, "metadata": json.dumps(metadata)},
                files={"file": ("delta.bin", literals, "application/octet-stream")},
            )

        results = await asyncio.gather(
            apply_delta_req(metadata1, literals1),
            apply_delta_req(metadata2, literals2),
        )

        # Both requests should complete (one succeeds, one gets 409)
        statuses = sorted([r.status_code for r in results])
        assert statuses == [200, 409], f"Expected one success and one conflict, got {statuses}"

        # The final file should be one of the two modifications (not corrupted)
        final_content = (bucket_dir / rel).read_bytes()
        assert final_content in (bytes(new1), bytes(new2)), (
            "File content should be one of the two modifications"
        )
