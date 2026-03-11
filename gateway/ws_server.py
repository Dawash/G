"""
WebSocket server for remote assistant control.

Starts on port 8765 (configurable). Accepts JSON commands,
routes them to the Brain, and returns results.

Requires: pip install websockets
"""

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765


class GatewayServer:
    """WebSocket gateway that bridges network clients to the Brain.

    Usage:
        server = GatewayServer(brain, config)
        server.start()  # Non-blocking, starts in background thread
        ...
        server.stop()
    """

    def __init__(self, brain, config=None):
        self._brain = brain
        self._config = config or {}
        self._port = self._config.get("gateway_port", DEFAULT_PORT)
        self._token = self._config.get("gateway_token", "")
        self._clients = set()
        self._thread = None
        self._loop = None
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gw")
        self._think_lock = None  # Created in async context

    def start(self):
        """Start the gateway server in a background thread."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.info("websockets not installed — gateway disabled. Install with: pip install websockets")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._run_server, daemon=True, name="gateway")
        self._thread.start()
        logger.info(f"Gateway server starting on port {self._port}")
        return True

    def stop(self):
        """Stop the gateway server."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._executor.shutdown(wait=False)
        logger.info("Gateway server stopped")

    def _run_server(self):
        """Run the async event loop in a background thread."""
        import websockets

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._think_lock = asyncio.Lock()

        async def _serve():
            try:
                server = await websockets.serve(self._handler, "0.0.0.0", self._port)
                logger.info(f"Gateway listening on ws://0.0.0.0:{self._port}")
                try:
                    while self._running:
                        await asyncio.sleep(1)
                finally:
                    server.close()
                    await server.wait_closed()
            except OSError as e:
                logger.warning(f"Gateway failed to start: {e}")

        try:
            self._loop.run_until_complete(_serve())
        except Exception as e:
            logger.error(f"Gateway error: {e}")
        finally:
            self._loop.close()

    async def _handler(self, websocket):
        """Handle a single WebSocket connection."""
        from gateway.protocol import parse_message, response_msg, error_msg

        client_id = id(websocket)
        authenticated = not self._token  # No token = no auth required

        logger.info(f"Gateway client connected: {client_id}")
        self._clients.add(websocket)

        try:
            async for raw in websocket:
                msg = parse_message(raw)
                if not msg:
                    await websocket.send(error_msg("", "Invalid JSON"))
                    continue

                msg_id = msg.get("id", "")
                msg_type = msg.get("type", "")

                # Authentication
                if msg_type == "auth":
                    if msg.get("token") == self._token or not self._token:
                        authenticated = True
                        await websocket.send(response_msg(msg_id, "Authenticated"))
                    else:
                        await websocket.send(error_msg(msg_id, "Invalid token"))
                    continue

                if not authenticated:
                    await websocket.send(error_msg(msg_id, "Not authenticated. Send auth first."))
                    continue

                # Route message
                try:
                    if msg_type == "think":
                        text = msg.get("text", "").strip()
                        if not text:
                            await websocket.send(error_msg(msg_id, "Empty text"))
                            continue
                        # Serialize brain.think() calls
                        async with self._think_lock:
                            result = await self._loop.run_in_executor(
                                self._executor, self._brain.think, text
                            )
                        await websocket.send(response_msg(msg_id, result or "No response"))

                    elif msg_type == "quick_chat":
                        text = msg.get("text", "").strip()
                        if not text:
                            await websocket.send(error_msg(msg_id, "Empty text"))
                            continue
                        result = await self._loop.run_in_executor(
                            self._executor, self._brain.quick_chat, text
                        )
                        await websocket.send(response_msg(msg_id, result or "No response"))

                    elif msg_type == "tool":
                        tool_name = msg.get("name", "")
                        tool_args = msg.get("args", {})
                        if not tool_name:
                            await websocket.send(error_msg(msg_id, "No tool name"))
                            continue
                        from brain import execute_tool
                        result = await self._loop.run_in_executor(
                            self._executor,
                            lambda: execute_tool(
                                tool_name, tool_args,
                                self._brain.action_registry if hasattr(self._brain, 'action_registry') else {},
                                self._brain.reminder_mgr if hasattr(self._brain, 'reminder_mgr') else None,
                                None  # speak_fn
                            )
                        )
                        await websocket.send(response_msg(msg_id, str(result) if result else "Done"))

                    elif msg_type == "status":
                        status = {
                            "connected_clients": len(self._clients),
                            "provider": getattr(self._brain, 'provider_name', 'unknown'),
                            "uptime": "running",
                        }
                        await websocket.send(response_msg(msg_id, json.dumps(status)))

                    else:
                        await websocket.send(error_msg(msg_id, f"Unknown message type: {msg_type}"))

                except Exception as e:
                    logger.error(f"Gateway handler error: {e}")
                    await websocket.send(error_msg(msg_id, f"Error: {e}"))

        except Exception:
            pass  # Client disconnected
        finally:
            self._clients.discard(websocket)
            logger.info(f"Gateway client disconnected: {client_id}")

    async def broadcast(self, event, **data):
        """Send an event to all connected clients."""
        from gateway.protocol import event_msg
        msg = event_msg(event, **data)
        for ws in list(self._clients):
            try:
                await ws.send(msg)
            except Exception:
                self._clients.discard(ws)

    @property
    def client_count(self):
        return len(self._clients)

    @property
    def is_running(self):
        return self._running
