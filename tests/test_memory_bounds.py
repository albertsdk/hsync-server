# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box memory-bound tests.

The RAM bound is deliberately generous: the point is to prove that a large
payload cannot grow the server's memory without limit, not to pin an exact
allocator figure (same convention as ``hsync-client/tests/test_memory_bounds.py``).
"""

from __future__ import annotations

import gc
import os
import tracemalloc
from pathlib import Path

import pytest
from hsync_common.delta import compute_signatures

pytestmark = pytest.mark.anyio

# 64 KiB: DELTA_BLOCK_SIZE_MEDIUM, the adaptive choice for a multi-MiB file.
_BLOCK = 64 * 1024


@pytest.fixture(autouse=True)
def _reset_delta_state():
    """Isolate the global token store and signature cache between tests."""
    from hsync_server.api.files.delta import signatures as sig_module
    from hsync_server.api.files.delta.tokens import delta_token_store

    delta_token_store._tokens.clear()
    sig_module._signature_cache.clear()
    yield
    for entry in list(delta_token_store._tokens.values()):
        delta_token_store._delete_payload(entry)
    delta_token_store._tokens.clear()
    sig_module._signature_cache.clear()


@pytest.fixture
def bucket_dir(temp_buckets_dir: str) -> Path:
    """A bucket ready for delta operations."""
    d = Path(temp_buckets_dir) / "bucket1"
    d.mkdir(parents=True)
    return d


def _client_sigs(tmp_path: Path, base: bytes, block_size: int) -> list[dict]:
    base_path = tmp_path / "client-base.bin"
    base_path.write_bytes(base)
    return [s.to_dict() for s in compute_signatures(str(base_path), block_size)]


class TestDeltaSignaturesMemory:
    async def test_large_literal_payload_is_not_buffered_in_ram(self, client, bucket_dir, tmp_path):
        """The literal payload must stream to disk, never sit in RAM.

        Regression for the previous ``find_matches_file`` + ``compress_data``
        path: an ~N MiB literal payload was materialised in RAM and then copied
        again for compression (≈2N). Streaming keeps the peak near the read /
        compression buffers regardless of N.
        """
        size = 8 * 1024 * 1024
        changed = int(size * 0.4)
        base = os.urandom(size)
        new = bytearray(base)
        # ~40% rewritten in place: a large literal payload that is still
        # worthwhile (well under DELTA_MAX_CHANGE_RATIO), so the accept path runs.
        new[:changed] = os.urandom(changed)
        rel = "docs/big.bin"
        target = bucket_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(new))

        sigs = _client_sigs(tmp_path, base, _BLOCK)

        tracemalloc.start()
        try:
            resp = await client.post(
                "/api/v1/buckets/bucket1/files/delta/signatures",
                json={"path": rel, "block_size": _BLOCK, "signatures": sigs},
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert resp.status_code == 200
        assert resp.json()["delta"] is True
        # ~3.2 MiB of literals: buffering them (plus the compressed copy) peaks
        # well above this; streaming stays in the low single-digit MiB.
        assert peak < 5 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MiB"


class TestListingStreaming:
    def test_listing_walk_is_lazy(self, tmp_path):
        """The listing walk must not materialise one dict per file.

        Regression for the previous ``file_list`` accumulation: pulling a single
        entry has to stay O(directory fanout + one entry). Materialising the tree
        would allocate ~15 MiB here.
        """
        from hsync_server.api.files import list as list_mod

        bucket_root = tmp_path / "bucket"
        for d in range(200):
            sub = bucket_root / "d" / f"{d:03d}"
            sub.mkdir(parents=True)
            for f in range(100):  # 20 000 files, fanout 100 per directory
                (sub / f"f{f:03d}.bin").write_bytes(b"x")

        gc.collect()
        tracemalloc.start()
        try:
            entries = list_mod._iter_file_entries(str(bucket_root), str(bucket_root), "bucket")
            next(entries)  # one entry is enough to prove the walk is lazy
            _, peak = tracemalloc.get_traced_memory()
            entries.close()
        finally:
            tracemalloc.stop()

        assert peak < 2 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MiB"

    async def test_listing_is_path_ordered(self, client, bucket_dir):
        """The stream is emitted in deterministic path order (mtime priority is
        applied by the consumer after it collects the whole array)."""
        for rel in ("b.txt", "a.txt", "d/c.txt"):
            target = bucket_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")

        resp = await client.get("/api/v1/buckets/bucket1/files")

        assert resp.status_code == 200
        paths = [entry["filepath"] for entry in resp.json()]
        assert paths == sorted(paths)
        assert set(paths) == {"a.txt", "b.txt", "d/c.txt"}
