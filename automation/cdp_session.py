"""
Persistent Chrome DevTools Protocol session.

Maintains a WebSocket connection to Chrome for reliable browser automation:
- Navigate to URLs
- Click elements by CSS selector
- Fill form fields
- Read page text/HTML
- Get current URL
- Wait for page load events
- Manage tabs (list, create, switch, close)
- Execute JavaScript
- Take screenshots

Usage:
    session = CDPSession()
    session.connect()
    session.navigate("https://google.com")
    session.fill("input[name=q]", "python tutorials")
    session.click("input[type=submit]")
    text = session.get_text()
    session.close()
"""

import json
import logging
import os
import subprocess
import threading
import time
import base64
from urllib.request import urlopen

logger = logging.getLogger(__name__)

CDP_PORT = 9222
CHROME_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]


class CDPSession:
    """Persistent Chrome DevTools Protocol session with event support."""

    def __init__(self, host="localhost", port=CDP_PORT):
        self._host = host
        self._port = port
        self._ws = None
        self._msg_id = 0
        self._pending = {}       # id -> {"event": Event, "result": None}
        self._event_handlers = {}  # method -> [callbacks]
        self._reader_thread = None
        self._connected = False
        self._lock = threading.Lock()

    def connect(self, tab_index=0):
        """Connect to a Chrome tab's WebSocket debugger."""
        tabs = self._get_tabs()
        if not tabs:
            raise ConnectionError("No Chrome tabs found. Is Chrome running with --remote-debugging-port?")

        # Find a page tab (not devtools, extension, etc.)
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            page_tabs = tabs

        target = page_tabs[min(tab_index, len(page_tabs) - 1)]
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ConnectionError(f"Tab has no WebSocket URL: {target.get('title', 'unknown')}")

        try:
            import websocket
            self._ws = websocket.create_connection(ws_url, timeout=10)
            self._connected = True
            self._start_reader()

            # Enable required domains
            self.send("Page.enable")
            self.send("DOM.enable")
            self.send("Runtime.enable")

            logger.info(f"CDP connected to: {target.get('title', 'unknown')}")
        except ImportError:
            raise ImportError("websocket-client required: pip install websocket-client")

    def send(self, method, params=None, timeout=10):
        """Send a CDP command and wait for response."""
        if not self._ws or not self._connected:
            raise ConnectionError("Not connected to Chrome")

        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id

        event = threading.Event()
        self._pending[msg_id] = {"event": event, "result": None, "error": None}

        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        self._ws.send(json.dumps(msg))

        if not event.wait(timeout=timeout):
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout}s")

        entry = self._pending.pop(msg_id, {})
        if entry.get("error"):
            raise RuntimeError(f"CDP error: {entry['error']}")
        return entry.get("result", {})

    def _start_reader(self):
        """Background thread reading WebSocket messages."""
        def _reader():
            while self._connected and self._ws:
                try:
                    raw = self._ws.recv()
                    if not raw:
                        break
                    msg = json.loads(raw)

                    if "id" in msg:
                        # Response to a command
                        pending = self._pending.get(msg["id"])
                        if pending:
                            if "error" in msg:
                                pending["error"] = msg["error"].get("message", str(msg["error"]))
                            else:
                                pending["result"] = msg.get("result", {})
                            pending["event"].set()
                    elif "method" in msg:
                        # Event notification
                        handlers = self._event_handlers.get(msg["method"], [])
                        for h in handlers:
                            try:
                                h(msg.get("params", {}))
                            except Exception:
                                pass
                except Exception:
                    if self._connected:
                        logger.debug("CDP reader error, connection may have closed")
                    break
            self._connected = False

        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()

    def subscribe(self, event_name, callback):
        """Subscribe to a CDP event (e.g., 'Page.loadEventFired')."""
        self._event_handlers.setdefault(event_name, []).append(callback)

    def _get_tabs(self):
        """Get list of debuggable tabs from Chrome."""
        try:
            url = f"http://{self._host}:{self._port}/json"
            resp = urlopen(url, timeout=3)
            return json.loads(resp.read())
        except Exception as e:
            logger.debug(f"Cannot reach Chrome CDP: {e}")
            return []

    def is_chrome_debuggable(self):
        """Check if Chrome is running with CDP enabled."""
        return len(self._get_tabs()) > 0

    def ensure_chrome(self):
        """Launch Chrome with remote debugging if not running."""
        if self.is_chrome_debuggable():
            return True

        chrome_path = None
        for p in CHROME_PATHS:
            if os.path.isfile(p):
                chrome_path = p
                break

        if not chrome_path:
            logger.warning("Chrome not found")
            return False

        try:
            subprocess.Popen(
                [chrome_path, f"--remote-debugging-port={self._port}",
                 "--no-first-run", "--no-default-browser-check"],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            # Wait for Chrome to start
            for _ in range(10):
                time.sleep(0.5)
                if self.is_chrome_debuggable():
                    logger.info("Chrome launched with CDP")
                    return True
            logger.warning("Chrome launched but CDP not responding")
            return False
        except Exception as e:
            logger.warning(f"Failed to launch Chrome: {e}")
            return False

    # ===== High-level browser actions =====

    def navigate(self, url, wait=True, timeout=15):
        """Navigate to a URL. Optionally wait for page load."""
        if wait:
            load_event = threading.Event()
            self.subscribe("Page.loadEventFired", lambda p: load_event.set())

        self.send("Page.navigate", {"url": url})

        if wait:
            load_event.wait(timeout=timeout)
        return True

    def get_url(self):
        """Get the current page URL."""
        result = self.send("Runtime.evaluate", {
            "expression": "window.location.href"
        })
        return result.get("result", {}).get("value", "")

    def get_title(self):
        """Get the current page title."""
        result = self.send("Runtime.evaluate", {
            "expression": "document.title"
        })
        return result.get("result", {}).get("value", "")

    def get_text(self, selector=None):
        """Get text content of the page or a specific element."""
        if selector:
            expr = f"document.querySelector('{selector}')?.innerText || ''"
        else:
            expr = "document.body?.innerText || ''"

        result = self.send("Runtime.evaluate", {"expression": expr})
        text = result.get("result", {}).get("value", "")
        # Limit output size
        return text[:5000] if text else ""

    def get_html(self, selector=None):
        """Get HTML of the page or a specific element."""
        if selector:
            expr = f"document.querySelector('{selector}')?.outerHTML || ''"
        else:
            expr = "document.documentElement?.outerHTML || ''"

        result = self.send("Runtime.evaluate", {"expression": expr})
        html = result.get("result", {}).get("value", "")
        return html[:10000] if html else ""

    def click(self, selector, timeout=5):
        """Click an element by CSS selector."""
        expr = f"""
        (function() {{
            let el = document.querySelector('{selector}');
            if (!el) return 'not_found';
            el.click();
            return 'clicked';
        }})()
        """
        result = self.send("Runtime.evaluate", {"expression": expr})
        value = result.get("result", {}).get("value", "")
        if value == "not_found":
            raise ValueError(f"Element not found: {selector}")
        return True

    def fill(self, selector, text):
        """Fill a form field by CSS selector."""
        # Escape text for JS
        escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        expr = f"""
        (function() {{
            let el = document.querySelector('{selector}');
            if (!el) return 'not_found';
            el.focus();
            el.value = '{escaped}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'filled';
        }})()
        """
        result = self.send("Runtime.evaluate", {"expression": expr})
        value = result.get("result", {}).get("value", "")
        if value == "not_found":
            raise ValueError(f"Element not found: {selector}")
        return True

    def run_js(self, expression):
        """Execute arbitrary JavaScript and return result."""
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value")

    def screenshot(self, format="png", quality=80):
        """Take a screenshot, returns base64-encoded image data."""
        params = {"format": format}
        if format == "jpeg":
            params["quality"] = quality
        result = self.send("Page.captureScreenshot", params, timeout=10)
        return result.get("data", "")

    def screenshot_to_file(self, path, format="png"):
        """Take a screenshot and save to file."""
        data = self.screenshot(format=format)
        if data:
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            return path
        return None

    def wait_for_element(self, selector, timeout=10, interval=0.5):
        """Wait until an element appears in the DOM."""
        start = time.time()
        while time.time() - start < timeout:
            expr = f"!!document.querySelector('{selector}')"
            result = self.send("Runtime.evaluate", {"expression": expr})
            if result.get("result", {}).get("value"):
                return True
            time.sleep(interval)
        raise TimeoutError(f"Element {selector} not found within {timeout}s")

    def get_tabs(self):
        """List all open tabs."""
        tabs = self._get_tabs()
        return [{"title": t.get("title", ""), "url": t.get("url", ""),
                 "id": t.get("id", "")} for t in tabs if t.get("type") == "page"]

    def switch_tab(self, tab_id):
        """Switch to a different tab by ID."""
        self.send("Target.activateTarget", {"targetId": tab_id})

    def new_tab(self, url="about:blank"):
        """Open a new tab."""
        result = self.send("Target.createTarget", {"url": url})
        return result.get("targetId")

    def close_tab(self, tab_id=None):
        """Close a tab (current if no ID given)."""
        if tab_id:
            self.send("Target.closeTarget", {"targetId": tab_id})
        else:
            self.send("Page.close")

    def back(self):
        """Navigate back."""
        self.run_js("history.back()")

    def forward(self):
        """Navigate forward."""
        self.run_js("history.forward()")

    def find_text(self, text):
        """Check if text exists on the page."""
        escaped = text.replace("'", "\\'")
        result = self.run_js(f"document.body?.innerText?.includes('{escaped}') || false")
        return bool(result)

    def close(self):
        """Close the CDP connection."""
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Module-level singleton
_session = None


def get_cdp_session():
    """Get or create the global CDP session."""
    global _session
    if _session is None or not _session._connected:
        _session = CDPSession()
    return _session
