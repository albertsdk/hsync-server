# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Literal-payload download tokens for the delta transfer endpoints.

When a download-delta request (POST ``/delta/signatures``) is accepted, the
server parks the literal payload in a temp file and hands the client a
single-use, short-lived token bound to ``(bucket, path)``; the client then
fetches the payload via GET ``/delta/data/{token}``. Tokens live in memory
(and payloads on disk, so multi-GB literals never sit in RAM); expired
entries are reaped lazily on every store access.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger as log

from hsync_server.config import get_buckets_path, get_delta_token_ttl
from hsync_server.metrics import update_active_tokens, update_temp_storage

# Hard cap on concurrently live tokens (DoS guard).
DELTA_MAX_ACTIVE_TOKENS = 1_000

# Hard cap on total temp file storage (10 GB) — prevents disk exhaustion from
# accumulated literal payloads when tokens are not consumed promptly.
DELTA_MAX_TEMP_STORAGE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

# Subdirectory of the buckets root holding literal payloads. It sits *outside*
# every bucket, so it is never picked up by sync listings.
_DELTA_TMP_DIRNAME = ".delta-tokens"


@dataclass(frozen=True)
class DeltaTokenEntry:
    """A parked literal payload awaiting download."""

    bucket: str
    payload_file: str  # absolute temp file holding the literal bytes
    compressed: bool  # payload is a single zstd frame
    created: float  # time.time() of issuance


def delta_tmp_dir() -> Path:
    """Return (creating if needed) the directory holding literal payloads."""
    tmp = Path(get_buckets_path()) / _DELTA_TMP_DIRNAME
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


class DeltaTokenStore:
    """In-memory token registry with TTL and lazy expiry."""

    def __init__(self, ttl: int | None = None):
        self._ttl = ttl
        # The registry is cross-thread by design: ``issue`` runs on asyncio
        # worker threads (asyncio.to_thread), while ``lookup``/``consume``/
        # ``discard``/``cleanup`` and the post-response payload cleanup
        # (``_delete_payload``) run on the event loop. The re-entrant lock
        # lets public methods compose (``issue`` → ``cleanup``) without
        # self-deadlock and makes the token-count/storage checks atomic.
        self._lock = threading.RLock()
        self._tokens: dict[str, DeltaTokenEntry] = {}
        # Lazy single-value cache of the total parked payload size. Exact by
        # construction: recomputed on the first access and invalidated at every
        # mutation (_delete_payload / cleanup / discard), so the steady-state
        # per-issue storage check is O(1) instead of one stat() per live token.
        self._storage_size: int | None = None

    @property
    def ttl(self) -> int:
        """Token time-to-live in seconds (late binding from config)."""
        return self._ttl if self._ttl is not None else get_delta_token_ttl()

    def _delete_payload(self, entry: DeltaTokenEntry) -> None:
        """Best-effort removal of the parked payload file."""
        with self._lock:
            try:
                Path(entry.payload_file).unlink(missing_ok=True)
            except OSError as exc:
                log.warning(f"Cannot remove delta payload {entry.payload_file}: {exc}")
            self._storage_size = None

    def cleanup(self, now: float | None = None) -> int:
        """Drop expired tokens and their payload files; return the count."""
        cutoff = (now if now is not None else time.time()) - self.ttl
        with self._lock:
            expired = [t for t, e in self._tokens.items() if e.created < cutoff]
            for token in expired:
                entry = self._tokens.pop(token)
                self._delete_payload(entry)
            return len(expired)

    def issue(self, *, bucket: str, payload: bytes, compressed: bool) -> str:
        """Park *payload* on disk and return its download token (UUID v4)."""
        with self._lock:
            self.cleanup()
            if len(self._tokens) >= DELTA_MAX_ACTIVE_TOKENS:
                raise RuntimeError("Too many active delta tokens")

            # Check total temp storage size before issuing new token
            total_storage = self._get_total_storage_size()
            if total_storage + len(payload) > DELTA_MAX_TEMP_STORAGE_BYTES:
                raise RuntimeError("Delta temp storage limit exceeded")

            token = uuid.uuid4().hex
            payload_file = str(delta_tmp_dir() / f"delta_{token}.bin")
            with open(payload_file, "wb") as f:
                f.write(payload)
            self._tokens[token] = DeltaTokenEntry(
                bucket=bucket,
                payload_file=payload_file,
                compressed=compressed,
                created=time.time(),
            )
            # The just-computed total + the new payload is the new exact size,
            # so the cache stays valid without a rescan (O(1) update).
            self._storage_size = total_storage + len(payload)

            update_active_tokens(len(self._tokens))
            update_temp_storage(total_storage + len(payload))

            return token

    def _get_total_storage_size(self) -> int:
        """Calculate total size of all parked payload files.

        The value is cached between mutations: the scan (one ``stat`` per live
        token) runs only after an invalidation, never on every ``issue``.
        """
        with self._lock:
            total = self._storage_size
            if total is None:
                total = 0
                for entry in self._tokens.values():
                    try:
                        total += Path(entry.payload_file).stat().st_size
                    except OSError:
                        pass
                self._storage_size = total
            return total

    def lookup(self, token: str) -> DeltaTokenEntry | None:
        """Return the entry for *token*, or None when unknown."""
        with self._lock:
            return self._tokens.get(token)

    def is_expired(self, entry: DeltaTokenEntry, now: float | None = None) -> bool:
        """Return True when the entry is older than the configured TTL."""
        return entry.created < (now if now is not None else time.time()) - self.ttl

    def consume(self, token: str) -> DeltaTokenEntry | None:
        """Remove and return the entry for *token* (single-use semantics)."""
        with self._lock:
            return self._tokens.pop(token, None)

    def discard(self, token: str) -> None:
        """Drop a token and its payload file without serving it."""
        with self._lock:
            entry = self._tokens.pop(token, None)
            if entry is not None:
                self._storage_size = None
                self._delete_payload(entry)


# Process-wide store shared by the endpoints.
delta_token_store = DeltaTokenStore()
