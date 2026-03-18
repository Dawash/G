"""
HUD Overlay server — FastAPI + WebSocket bridge between event bus and browser.

Architecture:
  Event bus (sync) → _event_queue (thread-safe) → WebSocket pump → browser
  Browser → WebSocket → bus.publish(Topics.HUD_COMMAND) → assistant_loop

Endpoints:
  GET  /           → index.html  (JARVIS HUD)
  GET  /health     → {"status": "ok", "clients": N}
  WS   /ws         → full-duplex event stream

Wire-up:
    from hud.server import start_hud_server
    start_hud_server(port=8767)    # daemon thread, non-blocking
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

try:
    from core.timeouts import Timeouts
except ImportError:
    Timeouts = None  # type: ignore

# ── optional FastAPI / uvicorn ────────────────────────────────────────────────
try:
    import uvicorn
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    _FASTAPI_OK = True
except ImportError:  # pragma: no cover
    _FASTAPI_OK = False
    FastAPI = WebSocket = WebSocketDisconnect = None  # type: ignore
    uvicorn = None  # type: ignore

_STATIC_DIR = Path(__file__).parent / "static"

# ── Bus (imported at module level for mockability in tests) ───────────────────
try:
    from core.event_bus import bus
    from core.topics import Topics as _Topics
except Exception:  # pragma: no cover
    bus = None   # type: ignore
    _Topics = None  # type: ignore

# ── Event queue — sync side writes, async side drains ────────────────────────
_event_queue: list[dict] = []
_eq_lock = threading.Lock()


def _enqueue(event_dict: dict) -> None:
    """Thread-safe enqueue from synchronous bus callbacks."""
    with _eq_lock:
        _event_queue.append(event_dict)
        # Cap at 200 to avoid unbounded growth if no clients connected
        if len(_event_queue) > 200:
            _event_queue.pop(0)


# ── Singleton server ──────────────────────────────────────────────────────────

class HudServer:
    """JARVIS HUD WebSocket server.

    Creates a FastAPI app, connects it to the event bus, and runs uvicorn in a
    background daemon thread.  Multiple browser clients are supported via a
    thread-safe set of active WebSocket connections.
    """

    def __init__(self, port: int = 8767) -> None:
        self.port = port
        self._app: "FastAPI | None" = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: Set["WebSocket"] = set()
        self._clients_lock = asyncio.Lock()  # async lock for client set
        self._clients_sync = threading.Lock()  # sync lock just for count
        self._started = False
        self._bus_subscribed = False

    # ── FastAPI app factory ───────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(title="G HUD", docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health():
            with self._clients_sync:
                n = len(self._clients)
            return JSONResponse({"status": "ok", "clients": n, "port": self.port})

        @app.get("/")
        async def index():
            html = _STATIC_DIR / "index.html"
            if html.exists():
                return FileResponse(str(html), media_type="text/html")
            return JSONResponse({"error": "index.html not found"}, status_code=404)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: "WebSocket"):
            await websocket.accept()
            async with self._clients_lock:
                self._clients.add(websocket)
            with self._clients_sync:
                pass  # count update happens via set len
            logger.info("HUD client connected (%d total)", len(self._clients))

            # Send a welcome snapshot immediately
            await self._send_snapshot(websocket)

            try:
                while True:
                    try:
                        data = await asyncio.wait_for(
                            websocket.receive_text(), timeout=30.0
                        )
                        await self._handle_client_message(data)
                    except asyncio.TimeoutError:
                        # Send heartbeat ping
                        try:
                            await websocket.send_text(json.dumps({"type": "ping"}))
                        except Exception:
                            break
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug("HUD WS error: %s", e)
            finally:
                async with self._clients_lock:
                    self._clients.discard(websocket)
                logger.info("HUD client disconnected (%d remaining)", len(self._clients))

        return app

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send_snapshot(self, ws: "WebSocket") -> None:
        """Send current awareness state to a newly connected client."""
        try:
            from core.awareness_state import awareness
            snapshot = awareness.snapshot()
            await ws.send_text(json.dumps({
                "type": "snapshot",
                "payload": snapshot,
                "ts": time.time(),
            }))
        except Exception as e:
            logger.debug("HUD snapshot send failed: %s", e)

    async def _handle_client_message(self, raw: str) -> None:
        """Route a message from the browser to the event bus."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "command":
            # Browser sends a voice-style command — publish to bus
            text = payload.get("text", "").strip()
            if text and bus is not None and _Topics is not None:
                try:
                    bus.publish(_Topics.HUD_COMMAND, {
                        "text": text,
                        "source": "hud",
                    }, source="hud_server")
                except Exception as e:
                    logger.debug("HUD command publish failed: %s", e)

        elif msg_type == "pong":
            pass  # heartbeat ack

    # ── Broadcast pump ───────────────────────────────────────────────────────

    async def _broadcast_pump(self) -> None:
        """Drain the thread-safe event queue and broadcast to all WS clients."""
        while True:
            await asyncio.sleep(0.05)  # 20 Hz polling
            with _eq_lock:
                if not _event_queue:
                    continue
                events = _event_queue.copy()
                _event_queue.clear()

            if not events:
                continue

            # Broadcast to all clients, removing dead connections
            async with self._clients_lock:
                dead: list = []
                for client in list(self._clients):
                    for ev in events:
                        try:
                            await client.send_text(json.dumps(ev))
                        except Exception:
                            dead.append(client)
                            break
                for d in dead:
                    self._clients.discard(d)

    # ── Bus subscriptions ─────────────────────────────────────────────────────

    def _subscribe_to_bus(self) -> None:
        """Subscribe to relevant topics and enqueue events for the HUD."""
        if self._bus_subscribed:
            return

        try:
            from core.event_bus import bus as _bus
            from core.topics import Topics

            def _make_handler(event_type: str):
                def _handler(event):
                    _enqueue({
                        "type": event_type,
                        "topic": event.topic,
                        "payload": event.payload,
                        "ts": time.time(),
                    })
                return _handler

            # Topics the HUD cares about
            _topics = [
                (Topics.SPEECH_RECOGNIZED, "speech"),
                (Topics.INPUT_RECEIVED,     "input"),
                (Topics.MODE_CLASSIFIED,    "mode"),
                (Topics.TOOL_CALLED,        "tool_called"),
                (Topics.TOOL_RESULT,        "tool_result"),
                (Topics.TOOL_ERROR,         "tool_error"),
                (Topics.RESPONSE_READY,     "response"),
                (Topics.TTS_SENTENCE,       "tts_sentence"),
                (Topics.TTS_COMPLETED,      "tts_done"),
                (Topics.TTS_INTERRUPTED,    "tts_interrupted"),
                (Topics.PROACTIVE_SUGGESTION, "proactive"),
                (Topics.PROACTIVE_SPEAK,    "proactive_urgent"),
                (Topics.CONTEXT_UPDATE,     "context"),
                (Topics.REMINDER_FIRED,     "reminder"),
                (Topics.STATE_IDLE,         "state_idle"),
                (Topics.STATE_ACTIVE,       "state_active"),
                (Topics.STARTUP_COMPLETE,   "startup"),
                (Topics.LOOP_ERROR,         "error"),
            ]

            for topic, ev_type in _topics:
                _bus.on(topic)(_make_handler(ev_type))

            self._bus_subscribed = True
            logger.debug("HUD subscribed to %d bus topics", len(_topics))

        except Exception as e:
            logger.warning("HUD bus subscription failed: %s", e)

    # ── Startup ───────────────────────────────────────────────────────────────

    def _run_server(self) -> None:
        """Target for the daemon thread — runs the uvicorn event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._app = self._build_app()

        # Schedule the broadcast pump as a background task
        async def _startup():
            self._loop.create_task(self._broadcast_pump())

        self._loop.run_until_complete(_startup())

        config = uvicorn.Config(
            app=self._app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        self._loop.run_until_complete(server.serve())

    def start(self) -> bool:
        """Start the HUD server in a background daemon thread.

        Returns:
            True if started successfully, False if FastAPI/uvicorn unavailable.
        """
        if not _FASTAPI_OK:
            logger.warning("HUD server requires: pip install fastapi uvicorn websockets")
            return False

        if self._started:
            return True

        self._subscribe_to_bus()

        self._thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="hud-server",
        )
        self._thread.start()
        self._started = True
        logger.info("HUD server started on http://localhost:%d", self.port)
        return True

    def stop(self) -> None:
        """Signal the server to stop (thread is daemon so it exits with process)."""
        self._started = False


# ── Module-level singleton + convenience function ─────────────────────────────

_hud_server: HudServer | None = None


def start_hud_server(port: int = 8767) -> bool:
    """Start the HUD server singleton.  Safe to call multiple times.

    Args:
        port: TCP port to listen on (default 8767).

    Returns:
        True if started (or already running), False if dependencies missing.
    """
    global _hud_server
    if _hud_server is None:
        _hud_server = HudServer(port=port)
    return _hud_server.start()


def get_hud_server() -> HudServer | None:
    """Return the running HUD server singleton, or None if not started."""
    return _hud_server


# ── Module-level FastAPI app accessor (builds a transient app for introspection) ─
def _get_app() -> "FastAPI | None":
    """Return the FastAPI app from the running server, or build a fresh one."""
    global _hud_server
    if _hud_server is not None and _hud_server._app is not None:
        return _hud_server._app
    if _FASTAPI_OK:
        return HudServer()._build_app()
    return None


app = _get_app() if _FASTAPI_OK else None
