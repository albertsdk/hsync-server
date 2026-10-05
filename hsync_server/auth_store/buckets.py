# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""Bucket inventory (FS ∪ membership) and deletion mixin."""

from __future__ import annotations

from pathlib import Path

from .db import AuthStoreBase

__all__ = ["BucketsMixin"]


def _iter_bucket_dirs(buckets_root: Path):
    """Yield bucket directory entries, skipping hidden names and non-directories."""
    for entry in buckets_root.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            yield entry


class BucketsMixin(AuthStoreBase):
    """Bucket listing/deletion operations (see ``DbMixin`` for the connection)."""

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
            for entry in _iter_bucket_dirs(buckets_root):
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
