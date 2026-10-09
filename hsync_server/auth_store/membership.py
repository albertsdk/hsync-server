# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Membership and ownership mixin: member lists, grants, transfers, reclaim."""

from __future__ import annotations

import sqlite3

from hsync_server.exceptions import ConflictError, ForbiddenError

from .db import AuthStoreBase
from .models import AccountRow, MemberRow, TargetNotFoundError

__all__ = ["MembershipMixin"]


class MembershipMixin(AuthStoreBase):
    """Membership/ownership operations (see ``DbMixin`` for the connection)."""

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

    def count_owned_buckets(self, account_id: str) -> int:
        """Return the number of buckets *account_id* owns (role='owner')."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM bucket_members WHERE client_id = ? AND role = 'owner'",
                (account_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

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

    def _require_owner_or_admin(
        self, bucket: str, caller_id: str, caller_is_admin: bool, action: str
    ) -> None:
        """Raise ForbiddenError unless *caller_id* is admin or owner of *bucket*."""
        if not caller_is_admin:
            role = self.get_member_role(bucket, caller_id)
            if role != "owner":
                raise ForbiddenError(f"Only the owner or admin can {action}.")

    def _assert_target_grantable(self, target: AccountRow, caller_id: str, action: str) -> None:
        """Raise ConflictError if *target* is disabled or is the caller itself."""
        if target.status != "active":
            raise ConflictError("Target account is disabled.")
        if target.id == caller_id:
            raise ConflictError(f"Cannot {action} to self.")

    def _upsert_owner(self, bucket: str, account_id: str) -> None:
        """Insert/update the owner row for *bucket* (inside an open transaction)."""
        self.conn.execute(
            """
            INSERT INTO bucket_members (bucket, client_id, role) VALUES (?, ?, 'owner')
            ON CONFLICT(bucket, client_id) DO UPDATE SET role = 'owner'
            """,
            (bucket, account_id),
        )

    def grant_member(
        self, bucket: str, target: AccountRow, caller_id: str, caller_is_admin: bool
    ) -> None:
        """Grant ``role=member`` to *target* on *bucket*.

        Caller must be admin or owner of *bucket*.
        """
        self._assert_target_grantable(target, caller_id, "grant membership")

        with self._lock:
            self._require_owner_or_admin(bucket, caller_id, caller_is_admin, "grant membership")

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
            self._require_owner_or_admin(bucket, caller_id, caller_is_admin, "revoke membership")

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
        self._assert_target_grantable(target, caller_id, "transfer ownership")

        with self._lock:
            try:
                self._begin()
                self._require_owner_or_admin(
                    bucket, caller_id, caller_is_admin, "transfer ownership"
                )

                current_owner = self.get_owner(bucket)
                if current_owner is not None and current_owner.id == target.id:
                    raise ConflictError("Target is already the owner.")

                if current_owner is not None:
                    self.conn.execute(
                        "UPDATE bucket_members SET role = 'member' WHERE bucket = ? AND client_id = ?",
                        (bucket, current_owner.id),
                    )
                self._upsert_owner(bucket, target.id)
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
                self._upsert_owner(bucket, target.id)
                self._commit()
            except sqlite3.IntegrityError:
                self._rollback()
                raise ConflictError("Concurrent ownership change.") from None
