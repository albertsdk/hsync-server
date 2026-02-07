"""Tests for the server SQLite version store (ADR-0025)."""

import os

from hsync_common.constants import HSYNCSTATE_FILENAME, HSYNCTOMBSTONES_FILENAME

from hsync_server.tombstones import get_tombstone_store, record_deletion
from hsync_server.tombstones import reset_cache as reset_ts
from hsync_server.versions import (
    get_server_cid,
    get_version_store,
    record_version,
    reset_cache,
    server_version_info,
)


class TestVersionStorePersistence:
    def test_record_survives_cache_reset(self, tmp_path):
        bucket = tmp_path / "photos"
        bucket.mkdir()
        path = bucket / "a.txt"
        path.write_text("hi")
        record_version(
            "photos", str(bucket), str(path), "ab" * 32, vclock={"Cid123456789012345678": 1}
        )
        reset_cache()
        info = server_version_info("photos", str(bucket), str(path))
        assert info is not None
        assert info["vclock"] == {"Cid123456789012345678": 1}
        assert get_version_store("photos").base_hash("a.txt") == "ab" * 32

    def test_no_sidecar_in_bucket(self, tmp_path):
        bucket = tmp_path / "photos"
        bucket.mkdir()
        path = bucket / "a.txt"
        path.write_text("hi")
        record_version("photos", str(bucket), str(path), "ab" * 32)
        record_deletion("photos", str(bucket), str(bucket / "gone.txt"), sha256sum="cd" * 32)
        names = {p.name for p in bucket.iterdir()}
        assert HSYNCSTATE_FILENAME not in names
        assert HSYNCTOMBSTONES_FILENAME not in names
        assert ".hsyncfolder" not in names

    def test_server_cid_persists_beside_db(self, tmp_path, monkeypatch):
        from hsync_server import versions

        state = tmp_path / "state"
        monkeypatch.setattr(versions, "get_state_dir", lambda: str(state))
        first = get_server_cid()
        assert len(first) == 21
        assert (state / ".hsyncnodeid").is_file()
        assert get_server_cid() == first

    def test_rebind_after_fork_drops_cache(self, tmp_path):
        bucket = tmp_path / "photos"
        bucket.mkdir()
        path = bucket / "a.txt"
        path.write_text("hi")
        record_version("photos", str(bucket), str(path), "ab" * 32)
        store = get_version_store("photos")
        from hsync_server import versions

        versions._pid = os.getpid() + 1
        reloaded = get_version_store("photos")
        assert reloaded is not store
        assert reloaded.base_hash("a.txt") == "ab" * 32

    def test_tombstone_roundtrip(self, tmp_path):
        bucket = tmp_path / "photos"
        bucket.mkdir()
        gone = bucket / "gone.txt"
        record_deletion("photos", str(bucket), str(gone), sha256sum="cd" * 32)
        reset_ts()
        entry = get_tombstone_store("photos").get("gone.txt")
        assert entry is not None
        assert entry["sha256"] == "cd" * 32
