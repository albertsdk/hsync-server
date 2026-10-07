# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Concurrency regression tests for shared server state.

Pin the synchronization that keeps blocking work off the event loop without
introducing cross-thread corruption:

1. ``_signature_cache`` (``delta/signatures.py``) — mutated by several
   ``asyncio.to_thread`` workers; an unguarded FIFO eviction could pop the
   same key twice (KeyError) or iterate a dict mutated mid-loop.
2. ``DeltaTokenStore`` (``delta/tokens.py``) — mutated from worker threads
   (``issue``), the event loop (``lookup``/``consume``/``discard``) and the
   post-response background cleanup; the token-cap and storage checks are
   check-then-act and must be atomic.
3. ``hsync_common.models.file._sha256_cache`` — read from server worker
   threads (listing/broadcast ``File.get`` offloads) and from the client's
   scan/transfer pools: the stale-entry drop must use the locked removal so
   two threads invalidating the same path never raise KeyError.
4. Delta literal compression (``delta/signatures.py``) — runs on
   ``asyncio.to_thread`` workers and never shares a zstd compressor
   instance across concurrent requests.

The tests are deliberate load hammers: an unguarded check-then-act fails
them probabilistically (crash assertion) while the synchronized code must
never raise and must preserve the documented invariants exactly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256 as sha256_hexdigest
from pathlib import Path

import pytest
from hsync_common.models import file as file_models
from hsync_common.models.file import File as LocalFile

from hsync_server.api.files.delta import signatures as signatures_module
from hsync_server.api.files.delta.tokens import DeltaTokenStore, delta_tmp_dir

_THREADS = 12
_ROUNDS = 25


def _digest(data: bytes) -> str:
    return sha256_hexdigest(data).hexdigest()


class TestSignatureCacheHardening:
    def test_concurrent_misses_and_eviction_never_raise(self, tmp_path: Path):
        """Hammer the cache past its FIFO cap from many threads.

        More distinct keys than ``_SIGNATURE_CACHE_MAX`` force constant
        eviction while repeated calls hit freshly-stored entries, so a
        check-then-pop race would crash with KeyError/RuntimeError on the
        unguarded version.
        """
        import random

        cap = signatures_module._SIGNATURE_CACHE_MAX
        key_count = cap * 2
        keys: list[tuple[str, str, int]] = []
        for i in range(key_count):
            p = tmp_path / f"base_{i:03d}.bin"
            data = bytes([i % 256]) * 1024
            p.write_bytes(data)
            keys.append((str(p), _digest(data), len(data)))

        def hammer(seed: int) -> None:
            rng = random.Random(seed)
            order = keys * 3
            rng.shuffle(order)
            for abs_path, sha, size in order:
                block_size, sigs = signatures_module._cached_signatures(abs_path, sha, size)
                assert sigs, "every computed cache entry must hold signatures"
                assert block_size > 0

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            for fut in [pool.submit(hammer, s) for s in range(_THREADS)]:
                fut.result()

        assert len(signatures_module._signature_cache) <= cap


class TestDeltaTokenStoreHardening:
    @pytest.fixture
    def store(self) -> DeltaTokenStore:
        return DeltaTokenStore(ttl=999_999)

    @pytest.fixture(autouse=True)
    def _clear_payload_dir(self, temp_buckets_dir):
        yield
        for leftover in delta_tmp_dir().glob("*.bin"):
            leftover.unlink(missing_ok=True)

    def _issue(self, store: DeltaTokenStore, i: int) -> str:
        return store.issue(bucket="bucket1", payload=f"payload-{i}".encode(), compressed=False)

    def test_concurrent_issue_lookup_consume_discard_never_raise(self, store):
        def worker(seed: int) -> None:
            for i in range(_ROUNDS):
                tok = self._issue(store, i)
                entry = store.lookup(tok)
                assert entry is not None
                assert entry.bucket == "bucket1"
                if seed % 2:
                    store.consume(tok)
                    store._delete_payload(entry)
                else:
                    store.discard(tok)

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            for f in [pool.submit(worker, s) for s in range(_THREADS)]:
                f.result()

        # Drain whatever the workers left: discard also unlinks the payload.
        for tok in list(store._tokens):
            store.discard(tok)
        assert not store._tokens
        assert store._get_total_storage_size() == 0

    def test_active_token_cap_is_atomic(self, store, monkeypatch):
        """The cap check is check-then-act: only the lock keeps it exact."""
        import hsync_server.api.files.delta.tokens as tokens_module

        monkeypatch.setattr(tokens_module, "DELTA_MAX_ACTIVE_TOKENS", 8)

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(self._issue, store, i) for i in range(_THREADS * 2)]
            results = []
            for f in futures:
                try:
                    results.append(f.result())
                except RuntimeError as exc:
                    # Saturation is the DoS guard firing: expected once the
                    # registry is full. Overshoot would mean the check-then-act
                    # interleaved (the unguarded behavior this test guards).
                    assert "Too many active delta tokens" in str(exc)

        # The serialised critical section admits exactly cap issuers.
        assert len(results) == 8
        assert len(store._tokens) == 8
        for tok in results:
            store.discard(tok)
        assert not any(delta_tmp_dir().glob("delta_*.bin"))

    def test_cleanup_concurrent_with_issue_never_raise(self, store):
        def issuer() -> None:
            for i in range(_ROUNDS):
                tok = self._issue(store, i)
                store.consume(tok)

        def cleaner() -> None:
            for _ in range(_ROUNDS):
                store.cleanup()

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(issuer) for _ in range(4)]
            futures += [pool.submit(cleaner) for _ in range(4)]
            for f in futures:
                f.result()

        for tok in list(store._tokens):
            store.consume(tok)


class TestSha256CacheHardening:
    def test_concurrent_stale_drops_never_raise(self, tmp_path: Path):
        """Two threads seeing the same stale entry must not double-delete.

        A raw ``del`` after the metadata mismatch lets two concurrent readers
        of one stale entry make the second delete raise KeyError inside
        ``File.get`` (outside its OSError handler).
        """
        # The SHA-256 cache is process-global: start from a known-clean state
        # so the final size assertion is exact (clean start / clean exit).
        file_models.clear_sha256_cache()
        try:
            self._run_stale_drop_race(tmp_path)
        finally:
            file_models.clear_sha256_cache()

    def _run_stale_drop_race(self, tmp_path: Path) -> None:
        target = tmp_path / "watched.txt"
        content = b"stable content"
        target.write_bytes(content)
        expected = _digest(content)

        def hammer() -> None:
            for _ in range(_ROUNDS * 4):
                # Same calling convention as the server listing: bare
                # filename + containing directory.
                obj = LocalFile.get(target.name, str(tmp_path))
                assert obj is not None
                assert obj.sha256sum == expected

        def stale_injector() -> None:
            for _ in range(_ROUNDS * 4):
                # Inject through the locked writer so the test itself honors
                # the synchronization contract it verifies. The identity
                # (mtime_ns, ctime_ns, size) is deliberately wrong so the entry
                # is stale against the real file and must be dropped on lookup.
                file_models._set_cached_sha256(str(target), "stale", 1, 1, 1)
                file_models.invalidate_sha256_cache(str(target))

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(hammer) for _ in range(_THREADS - 1)] + [
                pool.submit(stale_injector) for _ in range(2)
            ]
            for f in futures:
                f.result()

        # The cache ends fully consistent with the real file.
        assert LocalFile.get(target.name, str(tmp_path)) is not None
        assert len(file_models._sha256_cache) <= 1

    def test_concurrent_invalidate_and_get_never_raise(self, tmp_path: Path):
        file_models.clear_sha256_cache()
        try:
            self._run_invalidate_race(tmp_path)
        finally:
            file_models.clear_sha256_cache()

    def _run_invalidate_race(self, tmp_path: Path) -> None:
        paths = []
        for i in range(40):
            p = tmp_path / f"f_{i:02d}.bin"
            p.write_bytes(bytes([i % 256]) * 512)
            paths.append(p)

        def worker(seed: int) -> None:
            for _ in range(_ROUNDS):
                for p in paths:
                    LocalFile.get(str(p), str(tmp_path))
                for p in paths:
                    file_models.invalidate_sha256_cache(str(p))

        with ThreadPoolExecutor(max_workers=_THREADS * 2) as pool:
            for f in [pool.submit(worker, s) for s in range(_THREADS)]:
                f.result()

        with file_models._sha256_cache_lock:
            assert len(file_models._sha256_cache) <= file_models.SHA256_CACHE_MAX_SIZE


class TestDeltaLiteralCompressionHardening:
    """Delta literal compression must never go through a shared zstd instance.

    The endpoint compresses literals with ``compress_data`` on an
    ``asyncio.to_thread`` worker, whose instances are thread-local: two
    concurrent signature requests can never call ``compress()`` on one
    instance at the same time (which corrupts the payload and can segfault
    the interpreter). The production helper is hammered here so a shared
    instance fails loudly instead of poisoning streams.
    """

    def test_concurrent_literal_compression_roundtrips(self):
        from hsync_common.compression import decompress_data

        from hsync_server.api.files.delta import signatures as signatures_module

        payloads = [bytes([(i * 37) % 256]) * 200_000 for i in range(_THREADS)]

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(signatures_module._compress_literals, p) for p in payloads]
            compressed = [f.result() for f in futures]

        for payload, frame in zip(payloads, compressed, strict=True):
            assert decompress_data(frame, max_output_size=len(payload) + 1) == payload
