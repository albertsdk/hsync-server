"""
Tests for WebSocket events endpoint.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocket

from hsync_server.api.events import bucket_manager, ws_events
from hsync_server.config import get_compression_enabled
from hsync_server.constants import HSYNC_VERSION


class TestWebSocketAuthentication:
    """Tests for WebSocket authentication."""

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_auth_success(self, mock_websocket):
        """Successful authentication allows connection."""
        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-api-key"}',
            Exception("disconnect"),  # End the loop
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-api-key"):
            try:
                await ws_events(mock_websocket, "testbucket", "client1")
            except Exception:
                pass

        mock_websocket.accept.assert_called_once()
        mock_websocket.send_json.assert_called_with(
            {
                "type": "auth",
                "status": "ok",
                "version": HSYNC_VERSION,
                "compression_enabled": get_compression_enabled(),
            }
        )

    @pytest.mark.asyncio
    async def test_auth_invalid_token(self, mock_websocket):
        """Invalid token closes connection with 4003."""
        mock_websocket.receive_text.return_value = '{"type": "auth", "token": "wrong-key"}'

        with patch("hsync_server.api.events.HSYNC_API_KEY", "correct-key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        mock_websocket.close.assert_called_with(code=4003, reason="Invalid API key")

    @pytest.mark.asyncio
    async def test_auth_invalid_message_type(self, mock_websocket):
        """Invalid auth message type closes connection with 4001."""
        mock_websocket.receive_text.return_value = '{"type": "not-auth", "token": "key"}'

        with patch("hsync_server.api.events.HSYNC_API_KEY", "key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        mock_websocket.close.assert_called_with(code=4001, reason="Invalid auth message")

    @pytest.mark.asyncio
    async def test_auth_malformed_json(self, mock_websocket):
        """Malformed JSON during auth closes connection."""
        mock_websocket.receive_text.return_value = "not valid json"

        await ws_events(mock_websocket, "testbucket", "client1")

        mock_websocket.close.assert_called()


class TestWebSocketEvents:
    """Tests for WebSocket event processing."""

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        ws = AsyncMock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock()
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        return ws

    @pytest.fixture
    def auth_then_event(self):
        """Helper to create auth + event sequence."""

        def _create(event_json):
            from fastapi import WebSocketDisconnect

            return ['{"type": "auth", "token": "test-key"}', event_json, WebSocketDisconnect()]

        return _create

    @pytest.mark.asyncio
    async def test_invalid_event_name_ignored(self, mock_websocket, auth_then_event):
        """Invalid event names are ignored."""
        from fastapi import WebSocketDisconnect

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "invalid_event", "src": {}}',
            WebSocketDisconnect(),
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Should not raise, just log and continue

    @pytest.mark.asyncio
    async def test_malformed_event_json_ignored(self, mock_websocket):
        """Malformed event JSON is ignored."""
        from fastapi import WebSocketDisconnect

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            "not valid json",
            WebSocketDisconnect(),
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Should not raise, just log and continue

    @pytest.mark.asyncio
    async def test_client_disconnect_cleanup(self, mock_websocket):
        """Client disconnect removes from active connections."""
        from fastapi import WebSocketDisconnect

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            WebSocketDisconnect(),
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Client should be removed from connections
        manager = bucket_manager.get_manager("testbucket")
        assert "client1" not in manager.active_connections

    @pytest.mark.asyncio
    async def test_added_event_dispatched(self, mock_websocket, tmp_path):
        """Added event is dispatched to handler."""
        import os

        from fastapi import WebSocketDisconnect

        bucket_dir = str(tmp_path / "buckets" / "testbucket")
        os.makedirs(bucket_dir, exist_ok=True)

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "added", "src": {"path": "newdir", "permissions": "755"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"),
            patch("hsync_server.api.events.HSYNC_BUCKETS", str(tmp_path / "buckets")),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Directory should be created
        assert os.path.exists(f"{bucket_dir}/newdir")

    @pytest.mark.asyncio
    async def test_modified_event_dispatched(self, mock_websocket, tmp_path):
        """Modified event is dispatched to handler."""
        import os

        from fastapi import WebSocketDisconnect

        bucket_dir = str(tmp_path / "buckets" / "testbucket")
        target_dir = f"{bucket_dir}/existingdir"
        os.makedirs(target_dir, exist_ok=True)

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "modified", "src": {"path": "existingdir", "permissions": "700"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"),
            patch("hsync_server.api.events.HSYNC_BUCKETS", str(tmp_path / "buckets")),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

    @pytest.mark.asyncio
    async def test_deleted_event_dispatched(self, mock_websocket, tmp_path):
        """Deleted event is dispatched to handler."""
        import os

        from fastapi import WebSocketDisconnect

        bucket_dir = str(tmp_path / "buckets" / "testbucket")
        target_dir = f"{bucket_dir}/todelete"
        os.makedirs(target_dir, exist_ok=True)

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "deleted", "src": {"path": "todelete", "permissions": "755"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"),
            patch("hsync_server.api.events.HSYNC_BUCKETS", str(tmp_path / "buckets")),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Directory should be deleted
        assert not os.path.exists(target_dir)

    @pytest.mark.asyncio
    async def test_renamed_event_dispatched(self, mock_websocket, tmp_path):
        """Renamed event is dispatched to handler."""
        import os

        from fastapi import WebSocketDisconnect

        bucket_dir = str(tmp_path / "buckets" / "testbucket")
        src_dir = f"{bucket_dir}/olddir"
        os.makedirs(src_dir, exist_ok=True)

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "renamed", "src": {"path": "olddir", "permissions": "755"}, "dst": {"path": "newdir", "permissions": "755"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"),
            patch("hsync_server.api.events.HSYNC_BUCKETS", str(tmp_path / "buckets")),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Directory should be renamed
        assert not os.path.exists(src_dir)
        assert os.path.exists(f"{bucket_dir}/newdir")

    @pytest.mark.asyncio
    async def test_symlink_event_dispatched(self, mock_websocket, tmp_path):
        """Symlink event is dispatched to handler."""
        import os
        from pathlib import Path

        from fastapi import WebSocketDisconnect

        bucket_dir = str(tmp_path / "buckets" / "testbucket")
        os.makedirs(bucket_dir, exist_ok=True)
        target_file = f"{bucket_dir}/target.txt"
        Path(target_file).write_text("content")

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "symlink", "src": {"type": "symlink", "filepath": "link", "target": "target.txt"}}',
            WebSocketDisconnect(),
        ]

        with (
            patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"),
            patch("hsync_server.api.events.HSYNC_BUCKETS", str(tmp_path / "buckets")),
        ):
            await ws_events(mock_websocket, "testbucket", "client1")

        # Symlink should be created
        assert os.path.islink(f"{bucket_dir}/link")

    @pytest.mark.asyncio
    async def test_exception_during_event_processing(self, mock_websocket):
        """Exception during event processing is logged but doesn't crash."""
        from fastapi import WebSocketDisconnect

        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "added", "src": {"path": "test", "permissions": "invalid"}}',
            WebSocketDisconnect(),
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            # Should not raise
            await ws_events(mock_websocket, "testbucket", "client1")

    @pytest.mark.asyncio
    async def test_client_disconnect_with_error(self, mock_websocket):
        """Client disconnect with error is handled."""
        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            Exception("Connection error"),
        ]

        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            await ws_events(mock_websocket, "testbucket", "client1")

        mock_websocket.close.assert_called()
