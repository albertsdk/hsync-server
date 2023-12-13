"""
Tests for WebSocket events endpoint.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import WebSocket
from hsync_server.api.events import ws_events, bucket_manager


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
            Exception("disconnect")  # End the loop
        ]
        
        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-api-key"):
            try:
                await ws_events(mock_websocket, "testbucket", "client1")
            except Exception:
                pass
        
        mock_websocket.accept.assert_called_once()
        mock_websocket.send_json.assert_called_with({"type": "auth", "status": "ok"})

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
            return [
                '{"type": "auth", "token": "test-key"}',
                event_json,
                WebSocketDisconnect()
            ]
        return _create

    @pytest.mark.asyncio
    async def test_invalid_event_name_ignored(self, mock_websocket, auth_then_event):
        """Invalid event names are ignored."""
        from fastapi import WebSocketDisconnect
        
        mock_websocket.receive_text.side_effect = [
            '{"type": "auth", "token": "test-key"}',
            '{"name": "invalid_event", "src": {}}',
            WebSocketDisconnect()
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
            'not valid json',
            WebSocketDisconnect()
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
            WebSocketDisconnect()
        ]
        
        with patch("hsync_server.api.events.HSYNC_API_KEY", "test-key"):
            await ws_events(mock_websocket, "testbucket", "client1")
        
        # Client should be removed from connections
        manager = bucket_manager.get_manager("testbucket")
        assert "client1" not in manager.active_connections

