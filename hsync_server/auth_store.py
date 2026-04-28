# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertobasso/hsync

"""
Server-only account and membership store backed by ``syncstate.db``.

The tables ``clients`` and ``bucket_members`` are created by the server on
the same SQLite file used for causal versions (ADR-0025), but their schema
is **not** part of ``hsync_common.models.sqlstore._SCHEMA_SQL``: the common
store is mirrored by Android and must not carry server auth state.  All
access goes through this module so there is a single DRY path for REST, WS,
CLI and bootstrap.

Consumers: ``hsync_server.auth`` (verify + AuthContext), ``hsync_server.api``
(buckets, admin, members, events), ``hsync_server.__main__`` (CLI admin),
``hsync_server.main`` (lifespan bootstrap).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from hsync_common.models.sqlstore import get_state_db, reset_state_db_cache
from loguru import logger as log

from hsync_server.config import get_state_db_path
from hsync_server.exceptions import ConflictError, ForbiddenError, ResourceNotFoundError

__all__ = [
    "ACCOUNT_ID_ALPHABET",
    "ACCOUNT_ID_LEN",
    "BOOTSTRAP_ADMIN_ID",
    "BOOTSTRAP_ADMIN_NAME",
    "KEY_PREFIX_LEN",
    "SCRYPT_DKLEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SECRET_HEX_LEN",
    "AccountRow",
    "AuthStore",
    "ConflictError",
    "ForbiddenError",
    "MemberRow",
    "TargetNotFoundError",
    "get_auth_store",
    "mint_account_id",
    "mint_secret",
    "parse_token",
    "reset_auth_cache",
]

# =============================================================================
# Constants
# =============================================================================

TOKEN_PREFIX = "hs_"
ACCOUNT_ID_LEN = 21
ACCOUNT_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
SECRET_HEX_LEN = 64  # secrets.token_hex(32)
KEY_PREFIX_LEN = 8  # first 8 hex chars of the active secret

# O(1) membership set for account-id character validation: parse_token runs on
# the per-request auth hot path, where `c in <str>` would be a linear substring
# scan instead of a hash lookup.
_ACCOUNT_ID_SET: frozenset[str] = frozenset(ACCOUNT_ID_ALPHABET)

# scrypt parameters (named, not magic numbers; overridable for tests via env).
# Production: N=2**17, r=8, p=1 (~128 MB, sub-second verify).
# Tests: set HSYNC_SCRYPT_N=16 in env for speed.
SCRYPT_N = int(os.environ.get("HSYNC_SCRYPT_N", str(2**17)))
SCRYPT_R = int(os.environ.get("HSYNC_SCRYPT_R", "8"))
SCRYPT_P = int(os.environ.get("HSYNC_SCRYPT_P", "1"))
SCRYPT_DKLEN = 32
SALT_LEN = 16
# scrypt requires ~128*N*r bytes; with N=2^17, r=8 → ~128 MB.
SCRYPT_MAXMEM = int(os.environ.get("HSYNC_SCRYPT_MAXMEM", str(256 * 1024 * 1024)))

# Bootstrap
BOOTSTRAP_ADMIN_ID = "HsyncAdminBootstrap01"
BOOTSTRAP_ADMIN_NAME = "admin"

# Name validation
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# last_used_at throttle window (seconds)
LAST_USED_THROTTLE = 300  # 5 minutes

# Schema (server-only, NOT in sqlstore._SCHEMA_SQL)
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

# =============================================================================
# Data classes
# =============================================================================


@dataclass(frozen=True)
class AccountRow:
    """A row from the ``clients`` table (never contains the secret)."""

    id: str
    name: str
    role: str
    status: str
    key_hash: str
    key_prefix: str
    key_generation: int
    key_expires_at: int | None
    prior_key_hash: str | None
    prior_expires_at: int | None
    created_at: int
    last_used_at: int | None


@dataclass(frozen=True)
class MemberRow:
    """A row from the ``bucket_members`` table."""

    bucket: str
    client_id: str
    role: str


# =============================================================================
# Exceptions
# =============================================================================


class TargetNotFoundError(ResourceNotFoundError):
    """A referenced account or bucket does not exist (HTTP 404)."""


# =============================================================================
# Token helpers
# =============================================================================


def mint_account_id() -> str:
    """Generate a 21-char alphanumeric account id (same style as ``get_server_cid``).

    The reserved bootstrap id ``BOOTSTRAP_ADMIN_ID`` is never minted here.
    """
    while True:
        cid = "".join(
            ACCOUNT_ID_ALPHABET[b // 4 % len(ACCOUNT_ID_ALPHABET)]
            for b in secrets.token_bytes(ACCOUNT_ID_LEN)
        )
        if cid != BOOTSTRAP_ADMIN_ID:
            return cid


def mint_secret() -> str:
    """Generate a 64-char lowercase hex secret (``secrets.token_hex(32)``)."""
    return secrets.token_hex(32)


def parse_token(token: str) -> tuple[str, str] | None:
    """Parse ``hs_<account_id 21 alnum>_<secret 64 hex>`` → ``(id, secret)``.

    Returns ``None`` if the format is invalid.  The caller must run a dummy
    scrypt on the failure path to keep timing uniform.
    """
    if not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX) :]
    sep = rest.find("_")
    if sep != ACCOUNT_ID_LEN:
        return None
    account_id = rest[:sep]
    secret = rest[sep + 1 :]
    if len(secret) != SECRET_HEX_LEN:
        return None
    if not all(c in _ACCOUNT_ID_SET for c in account_id):
        return None
    # Length is already validated above (== SECRET_HEX_LEN), so the precompiled
    # {64} pattern is equivalent to the ad-hoc `[0-9a-f]+` fullmatch compiled
    # here on every request.
    if not _SECRET_HEX_RE.fullmatch(secret):
        return None
    return account_id, secret


def _hash_secret(secret: str, salt: bytes | None = None) -> str:
    """Return a PHC-style scrypt hash string for *secret*.

    Format: ``$scrypt$ln=<log2(N)>,r=<r>,p=<p>$<salt b64>$<hash b64>``
    """
    if salt is None:
        salt = secrets.token_bytes(SALT_LEN)
    derived = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    import base64

    return (
        f"$scrypt$ln={SCRYPT_N.bit_length() - 1},r={SCRYPT_R},p={SCRYPT_P}"
        f"${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(derived).decode()}"
    )


def _verify_secret(secret: str, phc_hash: str) -> bool:
    """Verify *secret* against a PHC-style scrypt hash (constant-time)."""
    import base64

    try:
        parts = phc_hash.split("$")
        if len(parts) != 5 or parts[1] != "scrypt":
            return False
        params = dict(p.split("=") for p in parts[2].split(","))
        n = 1 << int(params["ln"])
        r = int(params["r"])
        p = int(params["p"])
        salt = base64.b64decode(parts[3])
        expected = base64.b64decode(parts[4])
        derived = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(derived, expected)
    except (KeyError, ValueError, TypeError):
        return False


# Pre-computed dummy hash for timing-safe "account unknown" path.
_DUMMY_PHC = _hash_secret("hsync-dummy", secrets.token_bytes(SALT_LEN))


def _key_prefix(secret: str) -> str:
    """First ``KEY_PREFIX_LEN`` hex chars of the secret (for logs/audit)."""
    return secret[:KEY_PREFIX_LEN]


def validate_name(name: str) -> str:
    """Validate and return a normalised account name."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid account name '{name}': 1-64 chars, "
            "alphanumeric with . _ - (not starting with . _ -)."
        )
    return name


def validate_secret_hex(secret: str) -> str:
    """Validate a 64-char lowercase hex secret (for ``HSYNC_ADMIN_KEY``)."""
    if not _SECRET_HEX_RE.fullmatch(secret):
        raise ValueError("HSYNC_ADMIN_KEY must be exactly 64 lowercase hex characters.")
    return secret


def parse_expiry(expires_at: str | None, valid_days: int | None) -> int | None:
    """Convert ``expires_at`` (RFC3339 UTC) or ``valid_days`` to a Unix UTC timestamp.

    Raises ``ValueError`` when both are given, or when ``expires_at`` is not a
    valid timezone-aware timestamp. Returns ``None`` when neither is given.

    Consumers: ``hsync_server.api.admin`` (REST), ``hsync_server.__main__`` (CLI).
    """
    if expires_at is not None and valid_days is not None:
        raise ValueError("Provide at most one of 'expires_at' or 'valid_days'.")
    if expires_at is not None:
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid expires_at: {e}") from None
        if dt.tzinfo is None:
            raise ValueError("Timestamp must include timezone (UTC).")
        return int(dt.timestamp())
    if valid_days is not None:
        return int(time.time()) + valid_days * 86400
    return None


# =============================================================================
# Store
# =============================================================================

_pid: int | None = None
_store_cache: dict[str, AuthStore] = {}
_cache_lock = threading.Lock()


def _rebind_after_fork() -> None:
    """Drop inherited SQLite handles after a worker fork."""
    global _pid
    current = os.getpid()
    if _pid is None:
        _pid = current
        return
    if _pid != current:
        _store_cache.clear()
        reset_state_db_cache()
        _pid = current


def get_auth_store() -> AuthStore:
    """Return the cached :class:`AuthStore` for the installation DB."""
    _rebind_after_fork()
    path = get_state_db_path()
    with _cache_lock:
        store = _store_cache.get(path)
        if store is None:
            store = AuthStore(path)
            _store_cache[path] = store
        return store


def reset_auth_cache() -> None:
    """Clear the in-memory store cache (tests)."""
    with _cache_lock:
        for s in _store_cache.values():
            s.close()
        _store_cache.clear()


class AuthStore:
    """Account and membership store on ``syncstate.db``.

    All mutations run inside ``BEGIN IMMEDIATE`` transactions.  scrypt hashing
    is performed **outside** transactions so the write lock is held only for
    the short SQL phase.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        """Return the shared SQLite connection (cached by ``get_state_db``)."""
        return get_state_db(self._db_path).conn

    def close(self) -> None:
        """No-op: the underlying connection is managed by ``get_state_db``."""
        pass

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

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap_admin(self, env_secret: str | None) -> str | None:
        """Create the bootstrap admin account if the table is empty.

        Returns the full token ``hs_<id>_<secret>`` when a secret was
        generated (env was absent), or ``None`` when the env secret was used
        (silent) or the table was already populated.  Only the worker that
        actually inserts returns a non-None generated token.
        """
        with self._lock:
            count = self.conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            if count > 0:
                if env_secret:
                    log.warning(
                        "HSYNC_ADMIN_KEY is set but accounts already exist in DB; "
                        "the env value is ignored."
                    )
                return None

            if env_secret:
                validate_secret_hex(env_secret)
                secret = env_secret
                generated = False
            else:
                secret = mint_secret()
                generated = True

            key_hash = _hash_secret(secret)
            now = int(time.time())

            try:
                self._begin()
                # Re-check inside the transaction (race-safe multi-worker).
                count = self.conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
                if count > 0:
                    self._rollback()
                    return None

                self.conn.execute(
                    """
                    INSERT INTO clients
                      (id, name, role, status, key_hash, key_prefix,
                       key_generation, key_expires_at,
                       prior_key_hash, prior_expires_at,
                       created_at, last_used_at)
                    VALUES (?, ?, 'admin', 'active', ?, ?, 1, NULL, NULL, NULL, ?, NULL)
                    """,
                    (BOOTSTRAP_ADMIN_ID, BOOTSTRAP_ADMIN_NAME, key_hash, _key_prefix(secret), now),
                )

                # Adopt orphan bucket directories (bootstrap-only).
                self._adopt_orphan_buckets(BOOTSTRAP_ADMIN_ID)

                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                return None

            if generated:
                token = f"{TOKEN_PREFIX}{BOOTSTRAP_ADMIN_ID}_{secret}"
                # Print to stderr directly (not through logger) so the
                # one-time token doesn't persist in log files.
                import sys

                print(
                    f"\n*** Bootstrap admin account created. ***\n"
                    f"Token (save now, not recoverable): {token}\n",
                    file=sys.stderr,
                )
                log.warning("Bootstrap admin account created; token printed to stderr.")
                return token
            log.info("Bootstrap admin account created from HSYNC_ADMIN_KEY.")
            return None

    def _adopt_orphan_buckets(self, admin_id: str) -> None:
        """Assign ownership of every existing bucket directory to *admin_id*.

        Runs only during bootstrap (when ``clients`` was empty).  Directories
        starting with ``.`` are excluded.
        """
        from hsync_server.config import get_buckets_path

        buckets_root = Path(get_buckets_path())
        if not buckets_root.exists():
            return
        for entry in buckets_root.iterdir():
            if entry.name.startswith("."):
                continue
            if not entry.is_dir():
                continue
            existing = self.conn.execute(
                "SELECT 1 FROM bucket_members WHERE bucket = ?", (entry.name,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')",
                    (entry.name, admin_id),
                )
                log.info(f"Adopted orphan bucket '{entry.name}' to admin.")

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------

    def create_account(
        self,
        name: str,
        role: str = "client",
        expires_at: int | None = None,
    ) -> tuple[str, str]:
        """Create a new account. Returns ``(account_id, token)``."""
        validate_name(name)
        if role not in ("admin", "client"):
            raise ValueError(f"Invalid role '{role}', must be 'admin' or 'client'.")

        secret = mint_secret()
        key_hash = _hash_secret(secret)
        account_id = mint_account_id()
        now = int(time.time())

        with self._lock:
            try:
                self._begin()
                self.conn.execute(
                    """
                    INSERT INTO clients
                      (id, name, role, status, key_hash, key_prefix,
                       key_generation, key_expires_at,
                       prior_key_hash, prior_expires_at,
                       created_at, last_used_at)
                    VALUES (?, ?, ?, 'active', ?, ?, 1, ?, NULL, NULL, ?, NULL)
                    """,
                    (account_id, name, role, key_hash, _key_prefix(secret), expires_at, now),
                )
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                raise ConflictError(f"Account name '{name}' already exists.") from None

        token = f"{TOKEN_PREFIX}{account_id}_{secret}"
        return account_id, token

    def list_accounts(self) -> list[AccountRow]:
        """Return all accounts (never includes secrets)."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM clients ORDER BY created_at ASC").fetchall()
        return [self._row_to_account(r) for r in rows]

    def get_account(self, account_id: str) -> AccountRow | None:
        """Return a single account by id, or None."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM clients WHERE id = ?", (account_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def get_account_by_name(self, name: str) -> AccountRow | None:
        """Return a single account by name, or None."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()
        return self._row_to_account(row) if row else None

    def resolve_target(self, client_id: str | None, name: str | None) -> AccountRow:
        """Resolve exactly one of ``client_id``/``name`` to an account.

        Raises ``ValueError`` if both or neither are given.
        Raises ``TargetNotFoundError`` if the account does not exist.
        """
        if (client_id is None) == (name is None):
            raise ValueError("Provide exactly one of 'client_id' or 'name'.")
        if client_id is not None:
            acct = self.get_account(client_id)
        else:
            acct = self.get_account_by_name(name)  # type: ignore[arg-type]
        if acct is None:
            raise TargetNotFoundError("Account not found.")
        return acct

    def rotate_key(
        self,
        account_id: str,
        grace_seconds: int | None = None,
        expires_at: int | None = None,
    ) -> str:
        """Rotate the active key. Returns the new full token.

        ``grace_seconds``: if given (≥1), the current active key becomes the
        prior key accepted until ``now + grace_seconds``.  The previous prior
        key is always discarded (at most two secrets live).

        ``expires_at``: if given, sets the new key's expiry; otherwise no
        expiry (the new key does not inherit the old one).
        """
        new_secret = mint_secret()
        new_hash = _hash_secret(new_secret)
        now = int(time.time())

        with self._lock:
            row = self.conn.execute(
                "SELECT key_hash, key_generation, key_expires_at FROM clients WHERE id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise TargetNotFoundError("Account not found.")

            old_hash = row["key_hash"]
            old_gen = row["key_generation"]
            new_gen = old_gen + 1

            if grace_seconds and grace_seconds >= 1:
                prior_hash = old_hash
                prior_expires = now + grace_seconds
            else:
                prior_hash = None
                prior_expires = None

            self._begin()
            self.conn.execute(
                """
                UPDATE clients SET
                  key_hash = ?, key_prefix = ?, key_generation = ?,
                  key_expires_at = ?,
                  prior_key_hash = ?, prior_expires_at = ?
                WHERE id = ?
                """,
                (
                    new_hash,
                    _key_prefix(new_secret),
                    new_gen,
                    expires_at,
                    prior_hash,
                    prior_expires,
                    account_id,
                ),
            )
            self._commit()

        return f"{TOKEN_PREFIX}{account_id}_{new_secret}"

    def set_expiry(self, account_id: str, expires_at: int | None) -> None:
        """Set or clear (None) the active key expiry."""
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM clients WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                raise TargetNotFoundError("Account not found.")
            self._begin()
            self.conn.execute(
                "UPDATE clients SET key_expires_at = ? WHERE id = ?",
                (expires_at, account_id),
            )
            self._commit()

    def disable(self, account_id: str, *, force: bool = False) -> None:
        """Set status=disabled. Refuses if it would leave zero active admins."""
        self._set_status(account_id, "disabled", force=force)

    def enable(self, account_id: str) -> None:
        """Set status=active (idempotent)."""
        self._set_status(account_id, "active")

    def _set_status(self, account_id: str, status: str, *, force: bool = False) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT role, status FROM clients WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise TargetNotFoundError("Account not found.")

            current_status = row["status"]
            if current_status == status:
                return  # idempotent

            if status == "disabled" and row["role"] == "admin" and not force:
                self._begin()
                # Re-check inside transaction (race-safe).
                active_admins = self.conn.execute(
                    "SELECT COUNT(*) FROM clients WHERE role = 'admin' AND status = 'active'"
                ).fetchone()[0]
                if active_admins <= 1:
                    self._rollback()
                    raise ConflictError("Cannot disable the last active admin account.")
                self.conn.execute(
                    "UPDATE clients SET status = 'disabled' WHERE id = ?",
                    (account_id,),
                )
                self._commit()
            else:
                self.conn.execute(
                    "UPDATE clients SET status = ? WHERE id = ?",
                    (status, account_id),
                )

    def delete_account(
        self, account_id: str, *, cascade: bool = False, force: bool = False
    ) -> None:
        """Delete an account.

        Without ``cascade``: 409 if the account owns buckets.
        With ``cascade``: deletes owned buckets (DB rows only; FS in a separate
        step after commit — orphan dirs are reclaimable by admin).
        Refuses if it would leave zero active admins (unless ``force``).
        Last-admin check + deletion run in the same ``BEGIN IMMEDIATE``.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT role, status FROM clients WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise TargetNotFoundError("Account not found.")

            owned = self.conn.execute(
                "SELECT bucket FROM bucket_members WHERE client_id = ? AND role = 'owner'",
                (account_id,),
            ).fetchall()

            if owned and not cascade:
                raise ConflictError(
                    f"Account owns {len(owned)} bucket(s). Transfer ownership or use cascade=true."
                )

            self._begin()
            # Re-check last-admin inside the transaction (race-safe).
            if row["role"] == "admin" and row["status"] == "active" and not force:
                active_admins = self.conn.execute(
                    "SELECT COUNT(*) FROM clients WHERE role = 'admin' AND status = 'active'"
                ).fetchone()[0]
                if active_admins <= 1:
                    self._rollback()
                    raise ConflictError("Cannot delete the last active admin account.")

            if cascade:
                for r in owned:
                    bucket = r["bucket"]
                    self._delete_bucket_db(bucket)
            # ON DELETE CASCADE removes membership rows.
            self.conn.execute("DELETE FROM clients WHERE id = ?", (account_id,))
            self._commit()

        # FS cleanup after DB commit (orphan dirs are reclaimable).
        if cascade:
            for r in owned:
                self._delete_bucket_fs(r["bucket"])

    # ------------------------------------------------------------------
    # Verify (called by auth.py)
    # ------------------------------------------------------------------

    def verify_secret_with_kind(self, secret: str, account: AccountRow) -> str | None:
        """Verify *secret* and return 'active', 'prior', or None."""
        if _verify_secret(secret, account.key_hash):
            return "active"
        if account.prior_key_hash and account.prior_expires_at:
            now = int(time.time())
            if now < account.prior_expires_at and _verify_secret(secret, account.prior_key_hash):
                return "prior"
        return None

    def touch_last_used(self, account_id: str) -> None:
        """Update ``last_used_at`` if the throttle window has elapsed."""
        now = int(time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT last_used_at FROM clients WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                return
            last = row["last_used_at"]
            if last is not None and (now - last) < LAST_USED_THROTTLE:
                return
            self.conn.execute(
                "UPDATE clients SET last_used_at = ? WHERE id = ?",
                (now, account_id),
            )

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def get_members(self, bucket: str) -> list[MemberRow]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT bucket, client_id, role FROM bucket_members WHERE bucket = ?",
                (bucket,),
            ).fetchall()
        return [MemberRow(r["bucket"], r["client_id"], r["role"]) for r in rows]

    def get_owner(self, bucket: str) -> AccountRow | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT c.* FROM clients c
                JOIN bucket_members m ON m.client_id = c.id
                WHERE m.bucket = ? AND m.role = 'owner'
                """,
                (bucket,),
            ).fetchone()
        return self._row_to_account(row) if row else None

    def has_access(self, account_id: str, bucket: str, is_admin: bool) -> bool:
        """Check if *account_id* has data access to *bucket*."""
        if is_admin:
            return True
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM bucket_members WHERE bucket = ? AND client_id = ?",
                (bucket, account_id),
            ).fetchone()
        return row is not None

    def get_member_role(self, bucket: str, account_id: str) -> str | None:
        """Return 'owner', 'member' or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT role FROM bucket_members WHERE bucket = ? AND client_id = ?",
                (bucket, account_id),
            ).fetchone()
        return row["role"] if row else None

    def get_share_member_ids(self, bucket: str) -> set[str]:
        """Return the non-admin member ids of *bucket*.

        Admins are operators, not share members: presence counters and any
        other "who is in this share" surface must exclude them even when an
        admin also owns the bucket.
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT m.client_id FROM bucket_members m
                JOIN clients c ON c.id = m.client_id
                WHERE m.bucket = ? AND c.role != 'admin'
                """,
                (bucket,),
            ).fetchall()
        return {r["client_id"] for r in rows}

    def set_owner_on_create(self, bucket: str, account_id: str) -> None:
        """Insert the owner row when a bucket is created."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')",
                (bucket, account_id),
            )

    def grant_member(
        self, bucket: str, target: AccountRow, caller_id: str, caller_is_admin: bool
    ) -> None:
        """Grant ``role=member`` to *target* on *bucket*.

        Caller must be admin or owner of *bucket*.
        """
        if target.status != "active":
            raise ConflictError("Target account is disabled.")
        if target.id == caller_id:
            raise ConflictError("Cannot grant membership to self.")

        with self._lock:
            # Check caller is owner or admin.
            if not caller_is_admin:
                role = self.get_member_role(bucket, caller_id)
                if role != "owner":
                    raise ForbiddenError("Only the owner or admin can grant membership.")

            # Check target is not already owner.
            target_role = self.get_member_role(bucket, target.id)
            if target_role == "owner":
                raise ConflictError("Target is already the owner. Use transfer instead.")
            if target_role == "member":
                raise ConflictError("Target is already a member.")

            try:
                self._begin()
                self.conn.execute(
                    "INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'member')",
                    (bucket, target.id),
                )
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                raise ConflictError("Target is already a member.") from None

    def revoke_member(
        self, bucket: str, target_id: str, caller_id: str, caller_is_admin: bool
    ) -> None:
        """Revoke membership of *target_id* on *bucket*.

        The last owner cannot be revoked (use transfer first).
        Caller must be admin or owner of *bucket*.
        """
        with self._lock:
            if not caller_is_admin:
                role = self.get_member_role(bucket, caller_id)
                if role != "owner":
                    raise ForbiddenError("Only the owner or admin can revoke membership.")

            target_role = self.get_member_role(bucket, target_id)
            if target_role is None:
                raise TargetNotFoundError("Account is not a member of this bucket.")
            if target_role == "owner":
                raise ConflictError("Cannot revoke the owner. Transfer ownership first.")

            self.conn.execute(
                "DELETE FROM bucket_members WHERE bucket = ? AND client_id = ?",
                (bucket, target_id),
            )

    def transfer_owner(
        self,
        bucket: str,
        target: AccountRow,
        caller_id: str,
        caller_is_admin: bool,
    ) -> None:
        """Transfer ownership of *bucket* to *target*.

        Caller must be admin or current owner.  Target must be ``active`` and
        not already the owner.  Ex-owner is demoted to ``member``.  All
        authorization checks run inside ``BEGIN IMMEDIATE`` (race-safe).
        """
        if target.status != "active":
            raise ConflictError("Target account is disabled.")
        if target.id == caller_id:
            raise ConflictError("Cannot transfer ownership to self.")

        with self._lock:
            try:
                self._begin()
                # Re-check caller authorization inside the transaction.
                if not caller_is_admin:
                    role = self.get_member_role(bucket, caller_id)
                    if role != "owner":
                        raise ForbiddenError("Only the owner or admin can transfer ownership.")

                current_owner = self.get_owner(bucket)
                if current_owner is not None and current_owner.id == target.id:
                    raise ConflictError("Target is already the owner.")

                if current_owner is not None:
                    self.conn.execute(
                        "UPDATE bucket_members SET role = 'member' WHERE bucket = ? AND client_id = ?",
                        (bucket, current_owner.id),
                    )
                self.conn.execute(
                    """
                    INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')
                    ON CONFLICT(bucket, client_id) DO UPDATE SET role = 'owner'
                    """,
                    (bucket, target.id),
                )
                self._commit()
            except (sqlite3.IntegrityError, ForbiddenError, ConflictError):
                self._rollback()
                raise

    def reclaim_orphan(self, bucket: str, target: AccountRow, caller_is_admin: bool) -> None:
        """Claim an orphan bucket (directory exists, no owner) as admin.

        Only admin can reclaim orphans.  Target must be ``active``.
        """
        if not caller_is_admin:
            raise ForbiddenError("Only admin can reclaim orphan buckets.")
        if target.status != "active":
            raise ConflictError("Target account is disabled.")

        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM bucket_members WHERE bucket = ? AND role = 'owner'",
                (bucket,),
            ).fetchone()
            if existing is not None:
                raise ConflictError("Bucket already has an owner. Use transfer instead.")

            try:
                self._begin()
                self.conn.execute(
                    """
                    INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')
                    ON CONFLICT(bucket, client_id) DO UPDATE SET role = 'owner'
                    """,
                    (bucket, target.id),
                )
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                raise ConflictError("Concurrent ownership change.") from None

    # ------------------------------------------------------------------
    # Bucket inventory (FS ∪ membership)
    # ------------------------------------------------------------------

    def list_all_buckets(self) -> list[dict]:
        """Return a list of bucket dicts: ``{name, on_disk, owner, members}``.

        Merges filesystem directories with ``bucket_members`` rows.  Hidden
        directories (starting with ``.``) are excluded.
        """
        from hsync_server.config import get_buckets_path

        buckets_root = Path(get_buckets_path())
        fs_dirs: set[str] = set()
        if buckets_root.exists():
            for entry in buckets_root.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    fs_dirs.add(entry.name)

        with self._lock:
            member_rows = self.conn.execute(
                """
                SELECT m.bucket, m.client_id, m.role, c.name as client_name
                FROM bucket_members m
                LEFT JOIN clients c ON c.id = m.client_id
                ORDER BY m.bucket, m.role
                """
            ).fetchall()

        # Build bucket → members map.
        bucket_map: dict[str, dict] = {}
        for r in member_rows:
            b = r["bucket"]
            if b not in bucket_map:
                bucket_map[b] = {"name": b, "on_disk": b in fs_dirs, "owner": None, "members": []}
            member_entry: dict[str, str] = {
                "id": r["client_id"],
                "name": r["client_name"] if r["client_name"] else "?",
                "role": r["role"],
            }
            if r["role"] == "owner":
                bucket_map[b]["owner"] = member_entry
            else:
                bucket_map[b]["members"].append(member_entry)

        # Add FS-only orphans.
        for name in sorted(fs_dirs):
            if name not in bucket_map:
                bucket_map[name] = {"name": name, "on_disk": True, "owner": None, "members": []}

        return sorted(bucket_map.values(), key=lambda b: b["name"])

    # ------------------------------------------------------------------
    # Bucket deletion (admin only)
    # ------------------------------------------------------------------

    def _delete_bucket_db(self, bucket: str) -> None:
        """Delete all DB rows for *bucket* (membership + buckets table + versions)."""
        self.conn.execute("DELETE FROM bucket_members WHERE bucket = ?", (bucket,))
        # Delete from the lazy buckets table (name → folder_id).
        row = self.conn.execute(
            "SELECT folder_id FROM buckets WHERE bucket_name = ?", (bucket,)
        ).fetchone()
        if row is not None:
            folder_id = row["folder_id"]
            self.conn.execute("DELETE FROM versions WHERE folder_id = ?", (folder_id,))
            self.conn.execute("DELETE FROM tombstones WHERE folder_id = ?", (folder_id,))
            self.conn.execute("DELETE FROM folders WHERE folder_id = ?", (folder_id,))
            self.conn.execute("DELETE FROM buckets WHERE bucket_name = ?", (bucket,))

    @staticmethod
    def _delete_bucket_fs(bucket: str) -> None:
        """Remove the bucket directory from disk."""
        from shutil import rmtree

        from hsync_server.config import get_buckets_path

        bucket_dir = Path(get_buckets_path()) / bucket
        if bucket_dir.exists():
            rmtree(bucket_dir)

    def delete_bucket_db(self, bucket: str) -> None:
        """Delete all DB rows for *bucket* without touching the filesystem.

        Used to clean up "ghost" buckets: membership/versions rows that remain
        in the DB while the bucket directory no longer exists on disk.
        """
        with self._lock:
            self._begin()
            self._delete_bucket_db(bucket)
            self._commit()

    def delete_bucket(self, bucket: str) -> None:
        """Delete a bucket: DB rows + FS directory (admin only, checked by caller)."""
        self.delete_bucket_db(bucket)
        self._delete_bucket_fs(bucket)
