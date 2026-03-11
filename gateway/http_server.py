"""
Simple HTTP server to serve the web UI for phone/browser access.

Serves gateway/web_ui.html on port 8766 (one above the WebSocket port).
The web UI connects to the WebSocket gateway on port 8765.

Usage:
    server = WebUIServer(ws_port=8765, http_port=8766)
    server.start()  # Non-blocking, runs in background
    ...
    server.stop()
"""

import http.server
import logging
import os
import threading

logger = logging.getLogger(__name__)

_UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")


class _UIHandler(http.server.BaseHTTPRequestHandler):
    """Serves the web UI HTML file."""

    def do_GET(self):
        if self.path in ("/", "/index.html", "/ui"):
            try:
                with open(_UI_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
            except FileNotFoundError:
                self.send_error(404, "Web UI file not found")
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            # Redirect everything else to root
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP log spam."""
        logger.debug("HTTP: %s", format % args)


class WebUIServer:
    """HTTP server for the phone/browser web dashboard."""

    def __init__(self, http_port=8766):
        self._port = http_port
        self._server = None
        self._thread = None

    def start(self):
        """Start HTTP server in background thread."""
        try:
            self._server = http.server.HTTPServer(("0.0.0.0", self._port), _UIHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="web-ui",
            )
            self._thread.start()
            logger.info(f"Web UI serving on http://0.0.0.0:{self._port}")
            return True
        except OSError as e:
            logger.warning(f"Web UI server failed to start: {e}")
            return False

    def stop(self):
        """Stop the HTTP server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        logger.info("Web UI server stopped")

    @property
    def is_running(self):
        return self._server is not None

    @property
    def url(self):
        return f"http://0.0.0.0:{self._port}"
