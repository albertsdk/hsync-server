# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box concurrency stress for the server's public surfaces.

The behavioural BB suites drive one client at a time; this module produces the
load shape they never do — several clients hitting the same bucket in parallel
over loopback REST and WebSocket — and verifies only observable outcomes
(listings, per-file download re-hashes, relay fan-out). The scale is bounded so
the module fits the CI budget; the long-running load/RSS soak lives in
``bench/soak.py``.
"""

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.ws_helpers import connect_ws_auth, make_cid, recv_event, ws_base

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = [
    pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON),
    pytest.mark.usefixtures("bb_http"),
]

_THREADS = 6
_FILES_PER_THREAD = 8
_PAYLOAD_BYTES = 8 * 1024
_TIMEOUT = 120.0


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload(thread: int, item: int) -> bytes:
    """Deterministic per-file payload (no shared RNG across threads)."""
    seed = hashlib.sha256(f"{thread}:{item}".encode()).digest()
    return (seed * ((_PAYLOAD_BYTES // len(seed)) + 1))[:_PAYLOAD_BYTES]


def _cid(tag: str) -> str:
    """21-char alphanumeric cid for a REST/WS client of this module."""
    return f"Conc{tag}".ljust(21, "0")


def _client(live_server: tuple[str, str]) -> httpx.Client:
    """A per-thread authenticated client (never share one across threads)."""
    base, api_key = live_server
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )


def _run_all(fn, count: int, workers: int = _THREADS) -> list:
    """Run ``fn(i)`` on ``min(workers, count)`` threads; re-raise worker errors."""
    with ThreadPoolExecutor(max_workers=min(workers, count)) as pool:
        futures = [pool.submit(fn, i) for i in range(count)]
        return [f.result() for f in futures]


def _download(client: httpx.Client, bucket: str, rel: str) -> httpx.Response:
    """Download a file by its bucket-relative path, identity-encoded."""
    return client.get(
        f"/api/v1/buckets/{bucket}/files/download",
        params={"filename": rel},
        headers={"Accept-Encoding": "identity"},
    )


class TestParallelRestLoad:
    def test_parallel_upload_list_download_delete(self, live_server, bb_bucket, bb_upload):
        # Given six parallel clients, each owning an 8-file subtree, with the
        # expected bytes and the post-delete survivor set computed up front
        expected: dict[str, bytes] = {}
        for t in range(_THREADS):
            for i in range(_FILES_PER_THREAD):
                expected[f"t{t:02d}/file_{i:02d}.bin"] = _payload(t, i)
        deleted = {f"t{t:02d}/file_00.bin" for t in range(_THREADS)}
        survivors = {rel: data for rel, data in expected.items() if rel not in deleted}

        def worker(t: int) -> None:
            cid = _cid(f"Cli{t:02d}")
            with _client(live_server) as client:
                for i in range(_FILES_PER_THREAD):
                    rel = f"t{t:02d}/file_{i:02d}.bin"
                    r = bb_upload(
                        client,
                        bb_bucket,
                        rel.rsplit("/", 1)[-1],
                        expected[rel],
                        path=f"t{t:02d}",
                        cid=cid,
                    )
                    assert r.status_code == 201, r.text
                # List and download while the other threads are still writing.
                listing = client.get(f"/api/v1/buckets/{bb_bucket}/files")
                assert listing.status_code == 200, listing.text
                own = f"t{t:02d}/file_01.bin"
                dl = _download(client, bb_bucket, own)
                assert dl.status_code == 200, dl.text
                assert dl.content == expected[own]
                # Delete the first file of this thread's own tree.
                victim = f"t{t:02d}/file_00.bin"
                rd = client.request(
                    "DELETE",
                    f"/api/v1/buckets/{bb_bucket}/files",
                    data={"cid": cid, "filename": victim},
                )
                assert rd.status_code == 200, rd.text

        # When all clients upload, list, download and delete concurrently
        _run_all(worker, _THREADS)

        # Then the listing equals the survivors, with correct size and hash
        with _client(live_server) as client:
            listed = client.get(f"/api/v1/buckets/{bb_bucket}/files").json()
            by_path = {entry["filepath"]: entry for entry in listed}
            assert set(by_path) == set(survivors)
            for rel, data in survivors.items():
                assert by_path[rel]["size"] == len(data)
                assert by_path[rel]["sha256sum"] == _sha(data)

        items = list(survivors.items())

        # And every surviving file downloads byte-for-byte from a fresh client
        def verify(idx: int) -> None:
            rel, data = items[idx]
            with _client(live_server) as client:
                dl = _download(client, bb_bucket, rel)
                assert dl.status_code == 200, dl.text
                assert dl.content == data

        _run_all(verify, len(items), workers=8)


class TestConcurrentSamePathUploads:
    def test_duplicate_uploads_leave_one_consistent_file(self, live_server, bb_bucket, bb_upload):
        """Identical-content races on one path: many writers, one file, right bytes.

        The server may answer 201 (write won the race) or 409 (identical
        content detected after another writer landed it) — both are correct;
        what must never happen is a 5xx, a torn file, or a conflict copy.
        """
        # Given identical content and six clients racing on the same path
        data = _payload(9, 9)

        def worker(t: int) -> int:
            with _client(live_server) as client:
                r = bb_upload(client, bb_bucket, "dup.bin", data, cid=_cid(f"Dup{t:02d}"))
                assert r.status_code in (200, 201, 202, 409), r.text
                return r.status_code

        # When they upload it concurrently
        codes = _run_all(worker, _THREADS)
        assert any(code in (200, 201, 202) for code in codes), codes

        # Then exactly one consistent file remains (never a 5xx/torn/conflict copy)
        with _client(live_server) as client:
            listed = client.get(f"/api/v1/buckets/{bb_bucket}/files").json()
            assert [entry["filepath"] for entry in listed] == ["dup.bin"], listed
            assert listed[0]["sha256sum"] == _sha(data)
            dl = _download(client, bb_bucket, "dup.bin")
            assert dl.status_code == 200, dl.text
            assert dl.content == data


class TestWsRelayUnderParallelPublishers:
    async def test_every_publisher_event_reaches_subscriber(
        self, live_server: tuple[str, str], bb_bucket: str
    ):
        """Three publishers send concurrently; one subscriber receives all 24 events."""
        # Given one subscriber and three publishers in the same bucket
        ws_url = ws_base(live_server)
        api_key = live_server[1]
        publisher_count = 3
        per_publisher = 8

        subscriber, reply = await connect_ws_auth(ws_url, bb_bucket, make_cid("loadsub"), api_key)
        assert reply["status"] == "ok"
        publish_sockets: list[tuple[str, object]] = []
        try:
            for p in range(publisher_count):
                cid = make_cid(f"loadpub{p}")
                ws, auth = await connect_ws_auth(ws_url, bb_bucket, cid, api_key)
                assert auth["status"] == "ok"
                publish_sockets.append((cid, ws))

            # When the three publishers each emit 8 events concurrently
            async def publish(cid: str, ws) -> None:
                for i in range(per_publisher):
                    await ws.send(
                        json.dumps(
                            {
                                "name": "modified",
                                "cid": cid,
                                "bucket": bb_bucket,
                                "src": {
                                    "filepath": f"load/{cid}/f{i}.bin",
                                    "name": f"f{i}.bin",
                                    "size": 0,
                                    "sha256sum": "",
                                    "ctime": 0,
                                    "mtime": 0,
                                    "permissions": "644",
                                },
                                "dst": None,
                            }
                        )
                    )

            await asyncio.gather(*(publish(cid, ws) for cid, ws in publish_sockets))

            # Then the subscriber receives every (cid, filepath) event exactly once
            expected = {
                (cid, f"load/{cid}/f{i}.bin")
                for cid, _ws in publish_sockets
                for i in range(per_publisher)
            }
            received: set[tuple[str, str]] = set()
            while len(received) < len(expected):
                msg = await recv_event(subscriber, timeout=20.0)
                if msg.get("name") != "modified":
                    continue
                received.add((msg["cid"], msg["src"]["filepath"]))
            assert received == expected
        finally:
            await asyncio.gather(
                *(ws.close() for _cid_label, ws in publish_sockets), return_exceptions=True
            )
            await subscriber.close()
