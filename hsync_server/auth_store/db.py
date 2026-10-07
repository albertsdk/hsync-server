# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""SQLite connection, schema and transaction mixin for the auth store.

The tables ``clients`` and ``bucket_members`` live on the same SQLite file used
for causal versions (ADR-0025), but their schema is **not** part of
``hsync_common.models.sqlstore._SCHEMA_SQL``: the common store is mirrored by
Android and must not carry server auth state.
"""

from __future__ import annotations

import sqlite3
import threading

from hsync_common.models.sqlstore import get_state_db

from .models import AccountRow

__all__ = ["AuthStoreBase", "DbMixin"]


class AuthStoreBase:
    """Shared member surface of the store mixins (typing only).

    The mixins call these through ``self`` on the composed :class:`AuthStore`;
    ``DbMixin`` provides the implementations (``BucketsMixin`` provides the
    bucket-deletion pair).  Declaring them once here lets mypy type-check each
    mixin in isolation.
    """

    _db_path: str
    _lock: threading.RLock

    @property
    def conn(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _begin(self) -> None:
        raise NotImplementedError

    def _commit(self) -> None:
        raise NotImplementedError

    def _rollback(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> AccountRow:
        raise NotImplementedError

    def _delete_bucket_db(self, bucket: str) -> None:
        raise NotImplementedError

    @staticmethod
    def _delete_bucket_fs(bucket: str) -> None:
        raise NotImplementedError


# Schema owned by the server (not shared with the sync engine store)
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients (
  id               TEXT PRIMARY KEY,
  name             TEXT UNIQUE NOT NULL,
  role             TEXT NOT NULL CHECK (role IN ('admin','client')),
  status           TEXT NOT NULL CHECK (status IN ('active','disabled')),
  key_hash         TEXT NOT NULL,
  key_prefix       TEXT NOT NULL,
  key_generation   INTEGER NOT NULL DEFAULT 1,
  key_expires_at   INTEGER,
  prior_key_hash   TEXT,
  prior_expires_at INTEGER,
  created_at       INTEGER NOT NULL,
  last_used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS bucket_members (
  bucket     TEXT NOT NULL,
  client_id  TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('owner','member')),
  PRIMARY KEY (bucket, client_id)
);

-- Exactly one owner per bucket (partial unique index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_bucket_owner
  ON bucket_members (bucket) WHERE role = 'owner';
"""


class DbMixin(AuthStoreBase):
    """Connection, schema and transaction primitives shared by every mixin."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        """Return the shared SQLite connection (cached by ``get_state_db``)."""
        return get_state_db(self._db_path).conn

    def _ensure_schema(self) -> None:
        """Create auth tables if they don't exist (idempotent)."""
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> AccountRow:
        return AccountRow(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            status=row["status"],
            key_hash=row["key_hash"],
            key_prefix=row["key_prefix"],
            key_generation=row["key_generation"],
            key_expires_at=row["key_expires_at"],
            prior_key_hash=row["prior_key_hash"],
            prior_expires_at=row["prior_expires_at"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )
