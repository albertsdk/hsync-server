# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""A recursive delete is authoritative for its subtree (server side).

A client's recursive ``deleted`` event makes the server remove a whole tree. The
removal is synchronous on the event loop, but an upload or a delta streaming on
a worker thread can still publish a file inside it: ``rmtree`` then raises
``ENOTEMPTY``, the handler fails, the event is never broadcast and the tree stays
half present — the folder comes back on the peers' next reconcile.

The contract, shared with the P2P master (``hsync_common.deletions``):

- the tree is closed for the whole removal, and every writer (upload, delta,
  rename destination, symlink, ``added``/``modified`` event) is refused while it
  is open (409 on REST, dropped on the event path);
- the removal converges: a writer already past the gate is retried over, never
  reported as a half-deleted tree;
- the window closes with the removal, so it can never outlive it.
"""

import errno
import hashlib
import io
import os
import shutil
from unittest.mock import patch

import pytest
from hsync_common.deletions import begin_tree_delete, covered_by_tree_delete, reset_tree_deletes
from httpx import AsyncClient

from hsync_server.api.events_handlers.handlers import (
    handle_added_event,
    handle_deleted_event,
)
from hsync_server.exceptions import ConflictError

CID = "TestClientX1234567890"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with an empty in-flight-delete registry."""
    reset_tree_deletes()
    yield
    reset_tree_deletes()


class TestConvergingRmtree:
    def test_a_write_slipping_in_mid_removal_is_retried(self, tmp_path):
        tree = tmp_path / "Music"
        (tree / "i_o").mkdir(parents=True)
        (tree / "i_o" / "1.mp3").write_text("x")
        real_rmtree = shutil.rmtree
        calls = []

        def flaky(path, ignore_errors=False):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            return real_rmtree(path, ignore_errors=ignore_errors)

        from hsync_server.api.events_handlers.handlers import _rmtree_converging

        with patch("hsync_server.api.events_handlers.handlers.rmtree", flaky):
            _rmtree_converging(str(tree))

        assert len(calls) == 2
        assert not tree.exists()

    def test_a_persistent_failure_is_reported(self, tmp_path):
        tree = tmp_path / "Music"
        tree.mkdir()

        def always_failing(path, ignore_errors=False):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

        from hsync_server.api.events_handlers.handlers import _rmtree_converging

        with (
            patch("hsync_server.api.events_handlers.handlers.rmtree", always_failing),
            pytest.raises(OSError),
        ):
            _rmtree_converging(str(tree))


class TestDeleteEventOwnsItsWindow:
    @pytest.mark.asyncio
    async def test_the_tree_is_closed_for_the_whole_removal(self, tmp_path):
        tree = tmp_path / "Music"
        (tree / "i_o").mkdir(parents=True)
        (tree / "i_o" / "1.mp3").write_text("x")
        seen = {}

        def observing_rmtree(path):
            seen["covered"] = covered_by_tree_delete(str(tree / "i_o" / "1.mp3"))
            shutil.rmtree(path)

        with patch(
            "hsync_server.api.events_handlers.handlers._rmtree_converging", observing_rmtree
        ):
            event = await handle_deleted_event({"path": "Music"}, str(tmp_path), "test", CID)

        assert event is not None
        assert seen["covered"] is True
        assert not tree.exists()
        # The window never outlives the removal it guards.
        assert not covered_by_tree_delete(str(tree))

    @pytest.mark.asyncio
    async def test_a_failing_removal_still_reopens_the_tree(self, tmp_path):
        tree = tmp_path / "Music"
        tree.mkdir()

        def always_failing(path):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

        with (
            patch("hsync_server.api.events_handlers.handlers._rmtree_converging", always_failing),
            pytest.raises(OSError),
        ):
            await handle_deleted_event({"path": "Music"}, str(tmp_path), "test", CID)

        assert not covered_by_tree_delete(str(tree))


class TestEventPathRefusesInsideTheWindow:
    @pytest.mark.asyncio
    async def test_an_added_directory_inside_the_tree_is_dropped(self, tmp_path):
        begin_tree_delete(str(tmp_path / "Music"))
        event = await handle_added_event(
            {"path": "Music/Crono", "permissions": "755"}, str(tmp_path), "test", CID
        )
        assert event is None
        assert not (tmp_path / "Music").exists()

    @pytest.mark.asyncio
    async def test_a_sibling_directory_is_still_created(self, tmp_path):
        begin_tree_delete(str(tmp_path / "Music"))
        event = await handle_added_event(
            {"path": "Pictures/Crono", "permissions": "755"}, str(tmp_path), "test", CID
        )
        assert event is not None
        assert (tmp_path / "Pictures" / "Crono").is_dir()


class TestRestWritesRefuseInsideTheWindow:
    @pytest.mark.asyncio
    async def test_upload_inside_the_tree_is_refused(self, client: AsyncClient, temp_buckets_dir):
        bucket = "treeybucket"
        os.makedirs(os.path.join(temp_buckets_dir, bucket, "Music"), exist_ok=True)
        begin_tree_delete(os.path.join(temp_buckets_dir, bucket, "Music"))
        content = b"resurrected"

        response = await client.post(
            f"/api/v1/buckets/{bucket}/files",
            files={"file": ("1.mp3", io.BytesIO(content), "audio/mpeg")},
            data={
                "cid": CID,
                "path": "Music",
                "permissions": "644",
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
        )

        assert response.status_code == 409
        assert not os.path.exists(os.path.join(temp_buckets_dir, bucket, "Music", "1.mp3"))

    @pytest.mark.asyncio
    async def test_rename_destination_inside_the_tree_is_refused(
        self, client: AsyncClient, temp_buckets_dir
    ):
        bucket = "treeybucket2"
        bucket_path = os.path.join(temp_buckets_dir, bucket)
        os.makedirs(os.path.join(bucket_path, "Music"), exist_ok=True)
        source = os.path.join(bucket_path, "track.mp3")
        with open(source, "wb") as handle:
            handle.write(b"x")
        begin_tree_delete(os.path.join(bucket_path, "Music"))

        response = await client.patch(
            f"/api/v1/buckets/{bucket}/files",
            params={"cid": CID, "src": "track.mp3", "dst": "Music/track.mp3"},
        )

        assert response.status_code == 409
        assert os.path.exists(source)

    @pytest.mark.asyncio
    async def test_symlink_inside_the_tree_is_refused(self, client: AsyncClient, temp_buckets_dir):
        bucket = "treeybucket3"
        os.makedirs(os.path.join(temp_buckets_dir, bucket, "Music"), exist_ok=True)
        begin_tree_delete(os.path.join(temp_buckets_dir, bucket, "Music"))

        response = await client.post(
            f"/api/v1/buckets/{bucket}/symlinks",
            params={"cid": CID, "path": "Music/link", "target": "track.mp3"},
        )

        assert response.status_code == 409, response.text
        assert not os.path.lexists(os.path.join(temp_buckets_dir, bucket, "Music", "link"))

    @pytest.mark.asyncio
    async def test_a_delta_target_inside_the_tree_is_refused(self, temp_buckets_dir):
        from hsync_server.api.files.delta.apply import _resolve_delta_target

        bucket = "treeybucket4"
        bucket_path = os.path.join(temp_buckets_dir, bucket)
        os.makedirs(os.path.join(bucket_path, "Music"), exist_ok=True)
        with open(os.path.join(bucket_path, "Music", "1.mp3"), "wb") as handle:
            handle.write(b"base")
        begin_tree_delete(os.path.join(bucket_path, "Music"))

        with pytest.raises(ConflictError):
            await _resolve_delta_target(bucket, "Music/1.mp3")

    @pytest.mark.asyncio
    async def test_the_same_upload_works_once_the_delete_finished(
        self, client: AsyncClient, temp_buckets_dir
    ):
        bucket = "treeybucket5"
        os.makedirs(os.path.join(temp_buckets_dir, bucket, "Music"), exist_ok=True)
        begin_tree_delete(os.path.join(temp_buckets_dir, bucket, "Music"))
        reset_tree_deletes()
        content = b"fine"

        response = await client.post(
            f"/api/v1/buckets/{bucket}/files",
            files={"file": ("1.mp3", io.BytesIO(content), "audio/mpeg")},
            data={
                "cid": CID,
                "path": "Music",
                "permissions": "644",
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
        )

        assert response.status_code == 201
        assert os.path.exists(os.path.join(temp_buckets_dir, bucket, "Music", "1.mp3"))
