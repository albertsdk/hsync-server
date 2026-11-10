"""Tests for bucket provisioning ownership.

The admin CLI creates buckets as root on a packaged install; the directory must
end up owned by the account the server runs as, otherwise every upload fails
with ``EACCES``.
"""

from pathlib import Path

from hsync_server import bucket_provisioning
from hsync_server.auth_store import RENDEZVOUS_ADMIN_ID


def _buckets_root() -> Path:
    """The root the fixture points the provisioning layer at."""
    return Path(bucket_provisioning.get_buckets_path())


def _create(bucket: str) -> Path:
    bucket_provisioning.create_bucket(
        bucket, owner_account_id=RENDEZVOUS_ADMIN_ID, owner_name="admin"
    )
    return _buckets_root() / bucket


class TestBucketOwnership:
    def test_root_run_adopts_the_buckets_root_owner(self, monkeypatch):
        """A root CLI must not leave the bucket owned by root:root."""
        chowns: list[tuple[str, int, int]] = []
        monkeypatch.setattr(bucket_provisioning.os, "geteuid", lambda: 0)
        monkeypatch.setattr(
            bucket_provisioning.os,
            "chown",
            lambda path, uid, gid: chowns.append((str(path), uid, gid)),
        )

        created = _create("photos")

        root_stat = _buckets_root().stat()
        assert chowns == [(str(created), root_stat.st_uid, root_stat.st_gid)]

    def test_non_root_run_does_not_chown(self, monkeypatch):
        """The REST path runs as the service user: nothing to hand over."""
        chowns: list[tuple[str, int, int]] = []
        monkeypatch.setattr(bucket_provisioning.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(bucket_provisioning.os, "chown", lambda *args: chowns.append(args))

        created = _create("photos")

        assert created.is_dir()
        assert chowns == []
