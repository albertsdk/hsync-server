# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Concurrency tests for the per-bucket store registry (``store_registry.py``).

``PerBucketStores`` makes two promises the server's upload/delete hot paths
rely on: creation is memoized under contention (two threads can never build two
stores for one bucket) and a per-bucket ``RLock`` serializes every mutation of
that bucket's store. ``versions.record_version`` / ``tombstones.record_deletion``
take that lock around record+save, so parallel uploads or deletes of
*different* paths in one bucket must all land in ``syncstate.db`` — the load
shape a single-threaded request test can never produce.

Every test runs in the isolated temp state/buckets provided by the autouse
``fast_scrypt`` fixture, which also resets the registry and DB caches, so no
test can observe another one's rows.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hsync_server import tombstones, versions
from hsync_server.store_registry import PerBucketStores
from hsync_server.tombstones import record_deletion
from hsync_server.versions import get_version_store, record_version

_THREADS = 12
_ENTRIES = 20


def _digest(*parts: object) -> str:
    """Deterministic 64-hex digest for the given parts."""
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def _run_all(fn, count: int = _THREADS) -> list:
    """Run ``fn(i)`` on *count* threads; re-raise any worker exception."""
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(fn, i) for i in range(count)]
        return [f.result() for f in futures]


class TestPerBucketStoresContention:
    def test_factory_runs_once_and_locks_are_shared(self):
        registry: PerBucketStores[dict] = PerBucketStores()
        factory_calls: list[float] = []

        def factory() -> dict:
            factory_calls.append(time.monotonic())
            time.sleep(0.05)  # widen the check-then-act window
            return {}

        stores = _run_all(lambda _i: registry.get_or_create("bucket-a", factory))
        assert len(factory_calls) == 1
        assert all(store is stores[0] for store in stores)

        locks = _run_all(lambda _i: registry.lock("bucket-a"))
        assert all(lock is locks[0] for lock in locks)


class TestConcurrentVersionStoreWrites:
    def test_parallel_record_version_keeps_every_row(self, tmp_path: Path):
        bucket = "conc-versions"
        bucket_dir = tmp_path / "buckets" / bucket
        bucket_dir.mkdir(parents=True)
        expected: dict[str, str] = {}

        def writer(t: int) -> None:
            for i in range(_ENTRIES):
                rel = f"t{t:02d}/file_{i:03d}.bin"
                digest = _digest(t, rel)
                expected[rel] = digest
                record_version(bucket, str(bucket_dir), str(bucket_dir / rel), digest)

        _run_all(writer)

        assert len(expected) == _THREADS * _ENTRIES
        store = get_version_store(bucket)
        assert len(store) == len(expected)
        for rel, digest in expected.items():
            assert store.base_hash(rel) == digest

        # The rows must be in the database, not only in the memoized store:
        # rebuild the store from scratch and count again.
        versions.reset_cache()
        reloaded = get_version_store(bucket)
        assert len(reloaded) == len(expected)


class TestConcurrentTombstoneStoreWrites:
    def test_parallel_record_deletion_keeps_every_row(self, tmp_path: Path):
        bucket = "conc-tombstones"
        bucket_dir = tmp_path / "buckets" / bucket
        bucket_dir.mkdir(parents=True)
        expected: dict[str, str] = {}

        def writer(t: int) -> None:
            for i in range(_ENTRIES):
                rel = f"t{t:02d}/gone_{i:03d}.bin"
                digest = _digest("gone", t, rel)
                expected[rel] = digest
                record_deletion(bucket, str(bucket_dir), str(bucket_dir / rel), sha256sum=digest)

        _run_all(writer)

        assert len(expected) == _THREADS * _ENTRIES
        store = tombstones.get_tombstone_store(bucket)
        assert len(store) == len(expected)
        for rel in expected:
            assert store.get(rel) is not None

        tombstones.reset_cache()
        reloaded = tombstones.get_tombstone_store(bucket)
        assert len(reloaded) == len(expected)
