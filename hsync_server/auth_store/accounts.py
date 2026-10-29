# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Account lifecycle mixin: rendezvous, CRUD, key rotation, verify, last-used."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from loguru import logger as log

from hsync_server.exceptions import ConflictError

from .buckets import _iter_bucket_dirs
from .credentials import (
    LAST_USED_THROTTLE,
    RENDEZVOUS_ADMIN_ID,
    RENDEZVOUS_ADMIN_NAME,
    TOKEN_PREFIX,
    _hash_secret,
    _key_prefix,
    _verify_secret,
    mint_account_id,
    mint_secret,
    validate_name,
)
from .db import AuthStoreBase
from .models import AccountRow, TargetNotFoundError

__all__ = ["AccountsMixin"]


class AccountsMixin(AuthStoreBase):
    """Account-related operations (see ``DbMixin`` for the connection)."""

    # ------------------------------------------------------------------
    # Rendezvous
    # ------------------------------------------------------------------

    def rendezvous_admin(self, env_secret: str | None) -> str | None:
        """Create the rendezvous admin account if the table is empty.

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

                self._insert_client(
                    account_id=RENDEZVOUS_ADMIN_ID,
                    name=RENDEZVOUS_ADMIN_NAME,
                    role="admin",
                    key_hash=key_hash,
                    key_prefix=_key_prefix(secret),
                    key_expires_at=None,
                    created_at=now,
                )

                # Adopt orphan bucket directories (rendezvous-only).
                self._adopt_orphan_buckets(RENDEZVOUS_ADMIN_ID)

                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                return None

            if generated:
                token = f"{TOKEN_PREFIX}{RENDEZVOUS_ADMIN_ID}_{secret}"
                # The generated token is a full admin credential: never let it
                # reach a captured log stream. Interactive terminals get it on
                # stderr; daemons (systemd/journald, container log drivers) get
                # a 0600 file under the state dir instead.
                import os
                import sys

                from hsync_server.config import get_state_dir

                if sys.stderr.isatty():
                    print(
                        f"\n*** Rendezvous admin account created. ***\n"
                        f"Token (save now, not recoverable): {token}\n",
                        file=sys.stderr,
                    )
                    log.warning("Rendezvous admin account created; token printed to stderr.")
                else:
                    token_file = Path(get_state_dir()) / "rendezvous_admin_token"
                    fd = os.open(
                        token_file,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(token + "\n")
                    os.chmod(token_file, 0o600)
                    log.warning(
                        "Rendezvous admin account created; token written to "
                        f"{token_file} (mode 0600). Save it and delete the file."
                    )
                return token
            log.info("Rendezvous admin account created from HSYNC_ADMIN_KEY.")
            return None

    def _adopt_orphan_buckets(self, admin_id: str) -> None:
        """Assign ownership of every existing bucket directory to *admin_id*.

        Runs only during rendezvous (when ``clients`` was empty).  Directories
        starting with ``.`` are excluded.
        """
        from hsync_server.config import get_buckets_path

        buckets_root = Path(get_buckets_path())
        if not buckets_root.exists():
            return
        for entry in _iter_bucket_dirs(buckets_root):
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
                self._insert_client(
                    account_id=account_id,
                    name=name,
                    role=role,
                    key_hash=key_hash,
                    key_prefix=_key_prefix(secret),
                    key_expires_at=expires_at,
                    created_at=now,
                )
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                raise ConflictError(f"Account name '{name}' already exists.") from None

        token = f"{TOKEN_PREFIX}{account_id}_{secret}"
        return account_id, token

    def _insert_client(
        self,
        *,
        account_id: str,
        name: str,
        role: str,
        key_hash: str,
        key_prefix: str,
        key_expires_at: int | None,
        created_at: int,
    ) -> None:
        """Insert one active account row.

        Single definition of the ``clients`` column list shared by
        :meth:`rendezvous_admin` and :meth:`create_account` (both create an
        active account with a fresh generation-1 key); ``prior_*`` and
        ``last_used_at`` start NULL.
        """
        self.conn.execute(
            """
            INSERT INTO clients
              (id, name, role, status, key_hash, key_prefix,
               key_generation, key_expires_at,
               prior_key_hash, prior_expires_at,
               created_at, last_used_at)
            VALUES (?, ?, ?, 'active', ?, ?, 1, ?, NULL, NULL, ?, NULL)
            """,
            (
                account_id,
                name,
                role,
                key_hash,
                key_prefix,
                key_expires_at,
                created_at,
            ),
        )

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

    def resolve_account(self, id_or_name: str) -> AccountRow | None:
        """Resolve an account by id first, then by name, or None if neither matches.

        The single id-or-name lookup used by the admin/CLI surfaces (distinct from
        :meth:`resolve_target`, which requires exactly one explicit selector and
        raises on a miss).
        """
        return self.get_account(id_or_name) or self.get_account_by_name(id_or_name)

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

    def _assert_not_last_active_admin(self, action: str) -> None:
        """Raise ConflictError if *action* would leave zero active admins.

        Must run inside an open ``BEGIN IMMEDIATE`` (race-safe re-check).
        """
        active_admins = self.conn.execute(
            "SELECT COUNT(*) FROM clients WHERE role = 'admin' AND status = 'active'"
        ).fetchone()[0]
        if active_admins <= 1:
            self._rollback()
            raise ConflictError(f"Cannot {action} the last active admin account.")

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
                self._assert_not_last_active_admin("disable")
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
                self._assert_not_last_active_admin("delete")

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
