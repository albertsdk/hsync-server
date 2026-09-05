"""
Tests for WebSocket event handlers.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from hsync_common.path_validation import is_safe_symlink_target

import hsync_server.tombstones as tombstones
from hsync_server.api.events_handlers.handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.api.events_handlers.validation import is_path_within_bucket


@pytest.fixture(autouse=True)
def _isolate_tombstones(tmp_path):
    """Keep tombstone state inside the test tmp dir and reset the cache."""
    from hsync_common.constants import SYNCSTATE_DB_FILENAME

    tombstones.reset_cache()
    db_path = str(tmp_path / SYNCSTATE_DB_FILENAME)
    with patch.object(tombstones, "get_state_db_path", return_value=db_path):
        yield
    tombstones.reset_cache()


@pytest.fixture()
def _isolated_versions(tmp_path):
    """Keep version-store state inside the test tmp dir and reset the cache."""
    from hsync_common.constants import SYNCSTATE_DB_FILENAME

    from hsync_server import versions

    versions.reset_cache()
    db_path = str(tmp_path / SYNCSTATE_DB_FILENAME)
    with patch.object(versions, "get_state_db_path", return_value=db_path):
        yield
    versions.reset_cache()


class TestIsPathWithinBucket:
    """Tests for is_path_within_bucket function."""

    def test_valid_path_within_bucket(self, tmp_path):
        """Path within bucket returns True."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        path = f"{bucket_dir}/subdir/file.txt"
        assert is_path_within_bucket(path, bucket_dir) is True

    def test_path_traversal_rejected(self, tmp_path):
        """Path traversal attempt returns False."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        path = f"{bucket_dir}/../../../etc/passwd"
        assert is_path_within_bucket(path, bucket_dir) is False

    def test_empty_path_rejected(self, tmp_path):
        """Empty path returns False."""
        bucket_dir = str(tmp_path / "bucket")
        assert is_path_within_bucket("", bucket_dir) is False

    def test_none_path_rejected(self, tmp_path):
        """None path returns False."""
        bucket_dir = str(tmp_path / "bucket")
        assert is_path_within_bucket(None, bucket_dir) is False  # type: ignore[arg-type]

    def test_root_allowed_by_default(self, tmp_path):
        """Default behaviour: the bucket root itself is considered within the bucket."""
        bucket_dir = str(tmp_path)
        assert is_path_within_bucket(bucket_dir, bucket_dir) is True

    def test_root_rejected_when_allow_root_false(self, tmp_path):
        """allow_root=False rejects the bucket root to protect destructive ops.

        Regression test: a malicious or buggy client could send a delete
        event with path==bucket_dir which would result in ``rmtree`` on
        the whole bucket. The validation layer must reject the root
        when allow_root=False is passed.
        """
        bucket_dir = str(tmp_path)
        assert is_path_within_bucket(bucket_dir, bucket_dir, allow_root=False) is False

    def test_subpath_still_accepted_when_allow_root_false(self, tmp_path):
        """Subdirectories of the bucket remain valid when allow_root=False."""
        bucket_dir = str(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        assert is_path_within_bucket(str(sub), bucket_dir, allow_root=False) is True

    def test_absolute_path_outside_bucket(self, tmp_path):
        """Absolute path outside bucket returns False."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        assert is_path_within_bucket("/etc/passwd", bucket_dir) is False


class TestIsSafeSymlinkTarget:
    """Tests for is_safe_symlink_target function."""

    def test_relative_target_within_bucket(self, tmp_path):
        """Relative target within bucket is safe."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        symlink_path = f"{bucket_dir}/link"
        target = "file.txt"
        assert is_safe_symlink_target(symlink_path, target, bucket_dir) is True

    def test_absolute_target_within_bucket(self, tmp_path):
        """Absolute target within bucket is safe."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        symlink_path = f"{bucket_dir}/link"
        target = f"{bucket_dir}/file.txt"
        assert is_safe_symlink_target(symlink_path, target, bucket_dir) is True

    def test_relative_target_escapes_bucket(self, tmp_path):
        """Relative target escaping bucket is unsafe."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        symlink_path = f"{bucket_dir}/link"
        target = "../../../etc/passwd"
        assert is_safe_symlink_target(symlink_path, target, bucket_dir) is False

    def test_absolute_target_outside_bucket(self, tmp_path):
        """Absolute target outside bucket is unsafe."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        symlink_path = f"{bucket_dir}/link"
        target = "/etc/passwd"
        assert is_safe_symlink_target(symlink_path, target, bucket_dir) is False


class TestHandleAddedEvent:
    """Tests for handle_added_event function."""

    @pytest.mark.asyncio
    async def test_none_src_returns_none(self, tmp_path):
        """None src returns None."""
        result = await handle_added_event(None, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_directory_success(self, tmp_path):
        """Successfully add a directory."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "newdir", "permissions": "755"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert os.path.exists(f"{bucket_dir}/newdir")
        assert os.stat(f"{bucket_dir}/newdir").st_mode & 0o777 == 0o755

    @pytest.mark.asyncio
    async def test_add_directory_never_removes_owner_access(self, tmp_path):
        """Restrictive client directory perms must not break server storage access."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "restricted", "permissions": "000"}

        result = await handle_added_event(src, bucket_dir, "bucket", "cid")

        assert result is not None
        mode = os.stat(f"{bucket_dir}/restricted").st_mode & 0o777
        assert mode & 0o700 == 0o700

    @pytest.mark.asyncio
    async def test_add_directory_path_traversal_rejected(self, tmp_path):
        """Path traversal in directory add is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "../../../tmp/malicious", "permissions": "755"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_file_success(self, tmp_path):
        """Successfully add a file notification."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "file.txt", "sha256sum": "abc123", "permissions": "644"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is not None

    @pytest.mark.asyncio
    async def test_add_file_path_traversal_rejected(self, tmp_path):
        """Path traversal in file add is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "../../../etc/passwd", "sha256sum": "abc", "permissions": "644"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_file_clears_tombstone(self, tmp_path):
        """Re-adding a previously deleted path clears its tombstone (no resurrection)."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        store = tombstones.get_tombstone_store("bucket")
        store.record("file.txt", sha256sum="old", timestamp=1000)
        assert store.get("file.txt") is not None

        src = {"filepath": "file.txt", "sha256sum": "abc123", "permissions": "644"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert tombstones.get_tombstone_store("bucket").get("file.txt") is None

    @pytest.mark.asyncio
    async def test_add_empty_src_returns_none(self, tmp_path):
        """Empty src dict returns None."""
        bucket_dir = str(tmp_path / "bucket")
        result = await handle_added_event({}, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_directory_missing_permissions(self, tmp_path):
        """Directory without permissions returns None."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "newdir"}
        result = await handle_added_event(src, bucket_dir, "bucket", "cid")
        assert result is None


class TestHandleModifiedEvent:
    """Tests for handle_modified_event function."""

    @pytest.mark.asyncio
    async def test_none_src_returns_none(self, tmp_path):
        """None src returns None."""
        result = await handle_modified_event(None, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_modify_directory_success(self, tmp_path):
        """Successfully modify a directory."""
        bucket_dir = str(tmp_path / "bucket")
        target_dir = f"{bucket_dir}/existingdir"
        os.makedirs(target_dir)
        src = {"path": "existingdir", "permissions": "700"}
        result = await handle_modified_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert os.stat(target_dir).st_mode & 0o777 == 0o700

    @pytest.mark.asyncio
    async def test_modify_directory_path_traversal_rejected(self, tmp_path):
        """Path traversal in directory modify is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "../../../etc", "permissions": "777"}
        result = await handle_modified_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_modify_file_success(self, tmp_path):
        """Successfully modify a file notification."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "file.txt", "sha256sum": "abc123", "permissions": "644"}
        result = await handle_modified_event(src, bucket_dir, "bucket", "cid")
        assert result is not None

    @pytest.mark.asyncio
    async def test_modify_file_path_traversal_rejected(self, tmp_path):
        """Path traversal in file modify is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "../../../etc/passwd", "sha256sum": "abc", "permissions": "644"}
        result = await handle_modified_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_modify_file_clears_tombstone(self, tmp_path):
        """Modifying a previously deleted path clears its tombstone."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        store = tombstones.get_tombstone_store("bucket")
        store.record("file.txt", sha256sum="old", timestamp=1000)
        assert store.get("file.txt") is not None

        src = {"filepath": "file.txt", "sha256sum": "abc123", "permissions": "644"}
        result = await handle_modified_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert tombstones.get_tombstone_store("bucket").get("file.txt") is None

    @pytest.mark.asyncio
    async def test_modify_empty_src_returns_none(self, tmp_path):
        """Empty src dict returns None."""
        bucket_dir = str(tmp_path / "bucket")
        result = await handle_modified_event({}, bucket_dir, "bucket", "cid")
        assert result is None


class TestHandleDeletedEvent:
    """Tests for handle_deleted_event function."""

    @pytest.mark.asyncio
    async def test_none_src_returns_none(self, tmp_path):
        """None src returns None."""
        result = await handle_deleted_event(None, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_directory_success(self, tmp_path):
        """Successfully delete a directory."""
        bucket_dir = str(tmp_path / "bucket")
        target_dir = f"{bucket_dir}/todelete"
        os.makedirs(target_dir)
        src = {"path": "todelete", "permissions": "755"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert not os.path.exists(target_dir)

    @pytest.mark.asyncio
    async def test_delete_directory_not_found(self, tmp_path):
        """Delete non-existent directory still returns event."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "nonexistent", "permissions": "755"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is not None

    @pytest.mark.asyncio
    async def test_delete_directory_path_traversal_rejected(self, tmp_path):
        """Path traversal in directory delete is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "../../../tmp", "permissions": "755"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_file_success(self, tmp_path):
        """Successfully delete a file."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        file_path = f"{bucket_dir}/todelete.txt"
        Path(file_path).write_text("content")
        src = {"filepath": "todelete.txt", "sha256sum": "abc", "permissions": "644"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert not os.path.exists(file_path)

    @pytest.mark.asyncio
    async def test_delete_file_records_tombstone(self, tmp_path):
        """Deleting a file records a tombstone so offline peers do not resurrect it."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        file_path = f"{bucket_dir}/todelete.txt"
        Path(file_path).write_text("content")

        src = {"filepath": "todelete.txt", "sha256sum": "abc", "permissions": "644"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is not None

        entry = tombstones.get_tombstone_store("bucket").get("todelete.txt")
        assert entry is not None
        assert entry["ts"] > 0

    @pytest.mark.asyncio
    async def test_delete_file_path_traversal_rejected(self, tmp_path):
        """Path traversal in file delete is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "../../../etc/passwd", "sha256sum": "abc", "permissions": "644"}
        result = await handle_deleted_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_empty_src_returns_none(self, tmp_path):
        """Empty src dict returns None."""
        bucket_dir = str(tmp_path / "bucket")
        result = await handle_deleted_event({}, bucket_dir, "bucket", "cid")
        assert result is None


class TestHandleRenamedEvent:
    """Tests for handle_renamed_event function."""

    @pytest.mark.asyncio
    async def test_none_src_returns_none(self, tmp_path):
        """None src returns None."""
        result = await handle_renamed_event(None, {}, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_dst_returns_none(self, tmp_path):
        """None dst returns None."""
        result = await handle_renamed_event({}, None, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_directory_success(self, tmp_path):
        """Successfully rename a directory."""
        bucket_dir = str(tmp_path / "bucket")
        src_dir = f"{bucket_dir}/olddir"
        os.makedirs(src_dir)
        src = {"path": "olddir", "permissions": "755"}
        dst = {"path": "newdir", "permissions": "755"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is not None
        assert not os.path.exists(src_dir)
        assert os.path.exists(f"{bucket_dir}/newdir")

    @pytest.mark.asyncio
    async def test_rename_directory_src_path_traversal_rejected(self, tmp_path):
        """Path traversal in src directory is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"path": "../../../tmp", "permissions": "755"}
        dst = {"path": "newdir", "permissions": "755"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_directory_dst_path_traversal_rejected(self, tmp_path):
        """Path traversal in dst directory is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        src_dir = f"{bucket_dir}/olddir"
        os.makedirs(src_dir)
        src = {"path": "olddir", "permissions": "755"}
        dst = {"path": "../../../tmp/malicious", "permissions": "755"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_file_success(self, tmp_path):
        """Successfully rename a file."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src_file = f"{bucket_dir}/old.txt"
        Path(src_file).write_text("content")
        src = {"filepath": "old.txt", "sha256sum": "abc", "permissions": "644"}
        dst = {"filepath": "new.txt", "sha256sum": "abc", "permissions": "644"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is not None
        assert not os.path.exists(src_file)
        assert os.path.exists(f"{bucket_dir}/new.txt")

    @pytest.mark.asyncio
    async def test_rename_file_src_path_traversal_rejected(self, tmp_path):
        """Path traversal in src file is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"filepath": "../../../etc/passwd", "sha256sum": "abc", "permissions": "644"}
        dst = {"filepath": "new.txt", "sha256sum": "abc", "permissions": "644"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_file_dst_path_traversal_rejected(self, tmp_path):
        """Path traversal in dst file is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src_file = f"{bucket_dir}/old.txt"
        Path(src_file).write_text("content")
        src = {"filepath": "old.txt", "sha256sum": "abc", "permissions": "644"}
        dst = {"filepath": "../../../tmp/malicious.txt", "sha256sum": "abc", "permissions": "644"}
        result = await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_empty_src_returns_none(self, tmp_path):
        """Empty src dict returns None."""
        bucket_dir = str(tmp_path / "bucket")
        result = await handle_renamed_event({}, {"path": "dst"}, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_rename_file_moves_version_entry(self, tmp_path, _isolated_versions):
        """A file rename carries the version entry to the destination path."""
        from hsync_server.versions import record_version, server_version_info

        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src_file = f"{bucket_dir}/old.txt"
        Path(src_file).write_text("content")
        record_version(
            "bucket", bucket_dir, src_file, "ab" * 32, vclock={"Cid123456789012345678": 1}
        )

        src = {"filepath": "old.txt", "sha256sum": "ab" * 32, "permissions": "644"}
        dst = {"filepath": "new.txt", "sha256sum": "ab" * 32, "permissions": "644"}
        assert await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid") is not None

        moved = server_version_info("bucket", bucket_dir, f"{bucket_dir}/new.txt")
        assert moved is not None
        assert moved["vclock"] == {"Cid123456789012345678": 1}
        assert server_version_info("bucket", bucket_dir, src_file) is None

    @pytest.mark.asyncio
    async def test_rename_directory_moves_version_entries(self, tmp_path, _isolated_versions):
        """A directory rename carries the version entries of every child file."""
        from hsync_server.versions import record_version, server_version_info

        bucket_dir = str(tmp_path / "bucket")
        src_dir = f"{bucket_dir}/olddir"
        os.makedirs(src_dir)
        for name in ("a.txt", "b.txt"):
            child = f"{src_dir}/{name}"
            Path(child).write_text(name)
            record_version(
                "bucket", bucket_dir, child, "ab" * 32, vclock={"Cid123456789012345678": 1}
            )

        src = {"path": "olddir", "permissions": "755"}
        dst = {"path": "newdir", "permissions": "755"}
        assert await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid") is not None

        for name in ("a.txt", "b.txt"):
            moved = server_version_info("bucket", bucket_dir, f"{bucket_dir}/newdir/{name}")
            assert moved is not None, name
            assert moved["vclock"] == {"Cid123456789012345678": 1}
            assert server_version_info("bucket", bucket_dir, f"{src_dir}/{name}") is None

    @pytest.mark.asyncio
    async def test_rename_file_clears_destination_tombstone(self, tmp_path, _isolated_versions):
        """Renaming onto a previously deleted path drops the stale marker."""
        from hsync_common.models.tombstone import relpath_in_bucket

        from hsync_server.tombstones import get_tombstone_store, record_deletion

        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        Path(f"{bucket_dir}/old.txt").write_text("content")
        dst_file = f"{bucket_dir}/new.txt"
        record_deletion("bucket", bucket_dir, dst_file)

        store = get_tombstone_store("bucket")
        assert store.get(relpath_in_bucket(dst_file, bucket_dir)) is not None

        src = {"filepath": "old.txt", "sha256sum": "ab" * 32, "permissions": "644"}
        dst = {"filepath": "new.txt", "sha256sum": "ab" * 32, "permissions": "644"}
        assert await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid") is not None

        assert get_tombstone_store("bucket").get(relpath_in_bucket(dst_file, bucket_dir)) is None

    @pytest.mark.asyncio
    async def test_rename_directory_clears_destination_tombstones(
        self, tmp_path, _isolated_versions
    ):
        """Renaming a tree onto a deleted tree drops the children's stale markers."""
        from hsync_common.models.tombstone import relpath_in_bucket

        from hsync_server.tombstones import get_tombstone_store, record_deletion

        bucket_dir = str(tmp_path / "bucket")
        src_dir = f"{bucket_dir}/olddir"
        os.makedirs(src_dir)
        Path(f"{src_dir}/a.txt").write_text("a")
        dst_child = f"{bucket_dir}/newdir/a.txt"
        record_deletion("bucket", bucket_dir, dst_child)

        store = get_tombstone_store("bucket")
        assert store.get(relpath_in_bucket(dst_child, bucket_dir)) is not None

        src = {"path": "olddir", "permissions": "755"}
        dst = {"path": "newdir", "permissions": "755"}
        assert await handle_renamed_event(src, dst, bucket_dir, "bucket", "cid") is not None

        assert get_tombstone_store("bucket").get(relpath_in_bucket(dst_child, bucket_dir)) is None


class TestHandleSymlinkEvent:
    """Tests for handle_symlink_event function."""

    @pytest.mark.asyncio
    async def test_none_src_returns_none(self, tmp_path):
        """None src returns None."""
        result = await handle_symlink_event(None, str(tmp_path), "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_success(self, tmp_path):
        """Successfully create a symlink."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        target_file = f"{bucket_dir}/target.txt"
        Path(target_file).write_text("content")
        src = {"type": "symlink", "filepath": "link", "target": "target.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert os.path.islink(f"{bucket_dir}/link")

    @pytest.mark.asyncio
    async def test_symlink_path_traversal_rejected(self, tmp_path):
        """Path traversal in symlink path is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"type": "symlink", "filepath": "../../../tmp/link", "target": "target.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_target_traversal_rejected(self, tmp_path):
        """Path traversal in symlink target is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"type": "symlink", "filepath": "link", "target": "../../../etc/passwd"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_absolute_target_outside_bucket_rejected(self, tmp_path):
        """Absolute target outside bucket is rejected."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        src = {"type": "symlink", "filepath": "link", "target": "/etc/passwd"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_replaces_existing(self, tmp_path):
        """Symlink replaces existing file/symlink."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        link_path = f"{bucket_dir}/link"
        target1 = f"{bucket_dir}/target1.txt"
        target2 = f"{bucket_dir}/target2.txt"
        Path(target1).write_text("content1")
        Path(target2).write_text("content2")
        os.symlink(target1, link_path)
        src = {"type": "symlink", "filepath": "link", "target": "target2.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert os.readlink(link_path) == "target2.txt"

    @pytest.mark.asyncio
    async def test_symlink_creates_parent_dir(self, tmp_path):
        """Symlink creates parent directory if needed."""
        bucket_dir = str(tmp_path / "bucket")
        os.makedirs(bucket_dir)
        target_file = f"{bucket_dir}/target.txt"
        Path(target_file).write_text("content")
        src = {"type": "symlink", "filepath": "subdir/link", "target": "../target.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is not None
        assert os.path.islink(f"{bucket_dir}/subdir/link")

    @pytest.mark.asyncio
    async def test_symlink_wrong_type_returns_none(self, tmp_path):
        """Wrong type field returns None."""
        bucket_dir = str(tmp_path / "bucket")
        src = {"type": "file", "filepath": "link", "target": "target.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is None

    @pytest.mark.asyncio
    async def test_symlink_missing_type_returns_none(self, tmp_path):
        """Missing type field returns None."""
        bucket_dir = str(tmp_path / "bucket")
        src = {"filepath": "link", "target": "target.txt"}
        result = await handle_symlink_event(src, bucket_dir, "bucket", "cid")
        assert result is None
