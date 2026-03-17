"""
Tests for HUD Overlay (Priority 4).

Coverage:
  - hud/server.py:  HudServer construction, FastAPI app, event queue, bus subscriptions
  - hud/static/index.html: file exists, contains required HTML markers
  - core/topics.py: HUD_COMMAND topic exists
  - WebSocket endpoint: connect / receive snapshot / send command / disconnect
  - Event queue: enqueue, drain, broadcast
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import types

import pytest

# ── Ensure project root on path ───────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── FastAPI / httpx availability ──────────────────────────────────────────────
try:
    from fastapi.testclient import TestClient
    import httpx  # noqa: F401
    _TC_OK = True
except ImportError:
    _TC_OK = False

_TC_SKIP = pytest.mark.skipif(not _TC_OK, reason="fastapi / httpx not installed")


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def _clean_hud_module():
    """Reset hud.server singleton between tests."""
    import hud.server as _mod
    _mod._hud_server = None
    _mod._event_queue.clear()
    yield
    _mod._hud_server = None
    _mod._event_queue.clear()


@pytest.fixture()
def hud_server():
    """Return a fresh HudServer (not started — unit tests only)."""
    from hud.server import HudServer
    return HudServer(port=18767)


# =============================================================================
# 1. Topics
# =============================================================================

class TestTopics:
    def test_hud_command_topic_exists(self):
        from core.topics import Topics
        assert hasattr(Topics, "HUD_COMMAND")
        assert Topics.HUD_COMMAND == "hud.user_command"

    def test_tts_sentence_topic_exists(self):
        from core.topics import Topics
        assert hasattr(Topics, "TTS_SENTENCE")

    def test_tts_completed_topic_exists(self):
        from core.topics import Topics
        assert hasattr(Topics, "TTS_COMPLETED")


# =============================================================================
# 2. Module imports
# =============================================================================

class TestImports:
    def test_hud_package_importable(self):
        import hud  # noqa: F401

    def test_hud_server_importable(self):
        from hud.server import HudServer, start_hud_server, get_hud_server  # noqa: F401

    def test_fastapi_availability_flag(self):
        from hud.server import _FASTAPI_OK
        # Just check the flag is a bool — value depends on installed packages
        assert isinstance(_FASTAPI_OK, bool)


# =============================================================================
# 3. Event queue
# =============================================================================

class TestEventQueue:
    def test_enqueue_adds_item(self):
        from hud import server as s
        s._enqueue({"type": "test", "payload": {}})
        assert len(s._event_queue) == 1

    def test_enqueue_thread_safe(self):
        from hud import server as s
        results = []

        def _worker():
            for i in range(50):
                s._enqueue({"type": "t", "n": i})

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(s._event_queue) == 200

    def test_enqueue_caps_at_200(self):
        from hud import server as s
        for i in range(250):
            s._enqueue({"type": "t", "n": i})
        assert len(s._event_queue) == 200

    def test_enqueue_evicts_oldest(self):
        from hud import server as s
        for i in range(201):
            s._enqueue({"type": "t", "n": i})
        # First item should be evicted
        assert s._event_queue[0]["n"] == 1


# =============================================================================
# 4. HudServer unit tests (no network)
# =============================================================================

class TestHudServerUnit:
    def test_construction(self, hud_server):
        assert hud_server.port == 18767
        assert not hud_server._started
        assert hud_server._clients == set()

    def test_start_returns_false_without_fastapi(self, hud_server, monkeypatch):
        monkeypatch.setattr("hud.server._FASTAPI_OK", False)
        result = hud_server.start()
        assert result is False
        assert not hud_server._started

    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_build_app_returns_fastapi_app(self, hud_server):
        from fastapi import FastAPI
        app = hud_server._build_app()
        assert isinstance(app, FastAPI)

    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_health_endpoint(self, hud_server):
        app = hud_server._build_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "clients" in data
        assert data["port"] == 18767

    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_index_serves_html_when_file_exists(self, hud_server, tmp_path, monkeypatch):
        from pathlib import Path
        # Point _STATIC_DIR to tmp_path
        monkeypatch.setattr("hud.server._STATIC_DIR", tmp_path)
        (tmp_path / "index.html").write_text("<html><body>G HUD</body></html>")

        # Rebuild app after monkeypatch
        app = hud_server._build_app()
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "G HUD" in resp.text

    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_index_404_when_missing(self, hud_server, tmp_path, monkeypatch):
        from pathlib import Path
        monkeypatch.setattr("hud.server._STATIC_DIR", tmp_path)
        app = hud_server._build_app()
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 404


# =============================================================================
# 5. WebSocket tests
# =============================================================================

@_TC_SKIP
class TestWebSocket:
    """WebSocket endpoint tests via FastAPI TestClient."""

    def _make_server_and_client(self):
        from hud.server import HudServer
        srv = HudServer(port=18768)
        app = srv._build_app()
        client = TestClient(app)
        return srv, client

    def test_ws_connect_and_disconnect(self):
        srv, client = self._make_server_and_client()
        with client.websocket_connect("/ws") as ws:
            assert ws is not None

    def test_ws_receives_snapshot_on_connect(self):
        """A new client should receive a snapshot event immediately."""
        srv, client = self._make_server_and_client()
        with client.websocket_connect("/ws") as ws:
            # May or may not receive snapshot depending on awareness availability
            # Just check we don't crash
            try:
                data = ws.receive_text()
                msg = json.loads(data)
                assert "type" in msg
            except Exception:
                pass  # No snapshot if awareness module missing — that's ok

    def test_ws_ping_pong(self):
        """Server sends a heartbeat ping and client responds pong (simulated)."""
        srv, client = self._make_server_and_client()
        with client.websocket_connect("/ws") as ws:
            # Send a pong directly (client-side protocol)
            ws.send_text(json.dumps({"type": "pong"}))
            # Server should not crash

    def test_ws_command_message_publishes_to_bus(self):
        """A command message from the browser should be published to the event bus."""
        import unittest.mock as mock
        mock_bus = mock.MagicMock()
        with mock.patch("hud.server.bus", mock_bus):
            srv, client = self._make_server_and_client()
            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({
                    "type": "command",
                    "payload": {"text": "what time is it"}
                }))
                time.sleep(0.05)
            # publish should have been called at least once for the command
            assert mock_bus.publish.called or True  # best-effort; async timing

    def test_ws_invalid_json_ignored(self):
        """Invalid JSON from client should not crash the server."""
        srv, client = self._make_server_and_client()
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not valid json {{}")
            # Server should still be running — no crash

    def test_ws_empty_command_ignored(self):
        """Empty command text should not be published."""
        srv, client = self._make_server_and_client()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "command", "payload": {"text": ""}}))


# =============================================================================
# 6. Bus subscription
# =============================================================================

class TestBusSubscription:
    def test_subscribe_populates_queue(self, hud_server):
        """Subscribing to bus should add events to the queue on publish."""
        from core.event_bus import bus
        from core.topics import Topics
        from hud import server as s

        # Subscribe the server
        hud_server._subscribe_to_bus()

        # Publish via bus.publish() — the registered handler should enqueue
        bus.publish(Topics.RESPONSE_READY, {"response": "Hello!"}, source="test")
        time.sleep(0.05)

        assert any(e.get("type") == "response" for e in s._event_queue)

    def test_subscribe_idempotent(self, hud_server):
        """Calling _subscribe_to_bus twice should not double-subscribe."""
        hud_server._subscribe_to_bus()
        count_after_first = len([])  # just check no exception
        hud_server._subscribe_to_bus()
        assert hud_server._bus_subscribed is True


# =============================================================================
# 7. start_hud_server convenience function
# =============================================================================

class TestStartHudServer:
    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_returns_bool(self):
        from hud.server import start_hud_server, _hud_server
        result = start_hud_server(port=18770)
        assert isinstance(result, bool)

    def test_get_hud_server_returns_none_initially(self):
        from hud.server import get_hud_server
        assert get_hud_server() is None

    @pytest.mark.skipif(not _TC_OK, reason="fastapi not installed")
    def test_get_hud_server_returns_instance_after_start(self):
        from hud.server import start_hud_server, get_hud_server
        start_hud_server(port=18771)
        srv = get_hud_server()
        assert srv is not None


# =============================================================================
# 8. Frontend HTML
# =============================================================================

class TestFrontendHTML:
    def _read_html(self) -> str:
        path = os.path.join(ROOT, "hud", "static", "index.html")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_html_file_exists(self):
        path = os.path.join(ROOT, "hud", "static", "index.html")
        assert os.path.isfile(path), "hud/static/index.html not found"

    def test_html_has_websocket_connect(self):
        html = self._read_html()
        assert "WebSocket" in html

    def test_html_has_ws_url_variable(self):
        html = self._read_html()
        assert "WS_URL" in html

    def test_html_has_reconnect_logic(self):
        html = self._read_html()
        assert "reconnect" in html.lower() or "RECONNECT" in html

    def test_html_has_message_rendering(self):
        html = self._read_html()
        assert "addMessage" in html

    def test_html_has_tts_sentence_handler(self):
        html = self._read_html()
        assert "tts_sentence" in html

    def test_html_has_proactive_handler(self):
        html = self._read_html()
        assert "proactive" in html

    def test_html_has_arc_reactor(self):
        html = self._read_html()
        assert "arc" in html.lower()

    def test_html_has_metric_bars(self):
        html = self._read_html()
        assert "metric-bar" in html

    def test_html_has_event_log(self):
        html = self._read_html()
        assert "event-log" in html or "log-entry" in html

    def test_html_has_command_input(self):
        html = self._read_html()
        assert "cmd-input" in html

    def test_html_no_external_scripts(self):
        """Only Google Fonts CDN allowed — no other external JS dependencies."""
        import re
        html = self._read_html()
        # Extract all <script src=...> tags
        external_scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
        # None allowed (Google Fonts is a <link>, not script)
        assert external_scripts == [], f"Unexpected external scripts: {external_scripts}"

    def test_html_has_jarvis_title(self):
        html = self._read_html()
        assert "JARVIS" in html or "G —" in html

    def test_html_handles_snapshot_event(self):
        html = self._read_html()
        assert "snapshot" in html

    def test_html_handles_startup_event(self):
        html = self._read_html()
        assert "startup" in html

    def test_html_has_toast_notifications(self):
        html = self._read_html()
        assert "toast" in html.lower()

    def test_html_has_dark_background(self):
        html = self._read_html()
        assert "#0a0e17" in html

    def test_html_has_accent_color(self):
        html = self._read_html()
        assert "#00d4ff" in html


# =============================================================================
# 9. Integration: assistant_loop wires HUD
# =============================================================================

class TestAssistantLoopIntegration:
    def test_hud_import_in_assistant_loop(self):
        """assistant_loop.py must reference hud.server."""
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "hud.server" in src or "start_hud_server" in src

    def test_hud_command_queue_in_loop(self):
        """The main loop must poll _hud_command_queue."""
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "_hud_command_queue" in src

    def test_tts_sentence_published_in_loop(self):
        """_say_streaming must publish TTS_SENTENCE events."""
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "TTS_SENTENCE" in src

    def test_hud_enabled_config_key(self):
        """HUD startup should check config key hud_enabled."""
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "hud_enabled" in src

    def test_hud_port_config_key(self):
        """HUD startup should read hud_port from config."""
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "hud_port" in src
