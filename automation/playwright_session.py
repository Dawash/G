"""
Playwright-based browser session — replaces CDP for browser automation.

Benefits over CDP:
  - Cross-browser (Chrome, Firefox, WebKit)
  - Auto-wait for elements
  - Better selectors (CSS, XPath, text, role)
  - Built-in retry/timeout logic
  - Network interception

Usage:
    session = get_playwright_session()
    session.connect()
    session.navigate("https://google.com")
    session.fill("input[name=q]", "python tutorials")
    session.click(selector="input[type=submit]")
    text = session.get_text()
    session.close()
"""

import base64
import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

CDP_PORT = 9222
CHROME_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]


class PlaywrightSession:
    """Persistent Playwright browser session with thread-safe singleton access.

    Connects to an existing Chrome instance via CDP (port 9222) when possible,
    falls back to launching a new browser. Provides the same API as CDPSession
    for drop-in compatibility.
    """

    def __init__(self, host="localhost", port=CDP_PORT):
        self._host = host
        self._port = port
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._connected = False
        self._lock = threading.Lock()
        self._launched_chrome = False  # Track if we launched Chrome ourselves

    # =================================================================
    # Connection management
    # =================================================================

    def connect(self, tab_index=0):
        """Connect to a browser — tries existing Chrome CDP, then launches new.

        Args:
            tab_index: Which tab/page to target (0 = first).
        """
        with self._lock:
            if self._connected and self._page:
                try:
                    # Verify the page is still alive
                    self._page.title()
                    return
                except Exception:
                    self._connected = False

            self._init_playwright()

            # Strategy 1: Connect to existing Chrome via CDP
            if self._try_connect_existing(tab_index):
                return

            # Strategy 2: Launch Chrome with debugging, then connect
            if self.ensure_browser():
                if self._try_connect_existing(tab_index):
                    return

            # Strategy 3: Launch a fresh Playwright-managed browser
            self._launch_new_browser()

    def _init_playwright(self):
        """Lazy-initialize Playwright runtime."""
        if self._playwright is not None:
            return

        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
        except Exception as e:
            logger.error(f"Failed to start Playwright: {e}")
            raise ImportError(
                "Playwright not available. Install with: pip install playwright && playwright install chromium"
            ) from e

    def _try_connect_existing(self, tab_index=0):
        """Try to connect to an already-running Chrome via CDP.

        Returns:
            True if connection succeeded.
        """
        try:
            cdp_url = f"http://{self._host}:{self._port}"
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                pages = self._context.pages
                if pages:
                    idx = min(tab_index, len(pages) - 1)
                    self._page = pages[idx]
                    self._connected = True
                    logger.info(
                        f"Playwright connected to existing Chrome: "
                        f"{self._page.title()}"
                    )
                    return True
            # Browser connected but no pages — open one
            if self._context is None:
                self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self._page = self._context.new_page()
            self._connected = True
            logger.info("Playwright connected to Chrome (opened new page)")
            return True
        except Exception as e:
            logger.debug(f"Playwright CDP connect failed: {e}")
            # Clean up partial connection
            self._browser = None
            self._context = None
            self._page = None
            return False

    def _launch_new_browser(self):
        """Launch a Playwright-managed Chromium browser as last resort."""
        try:
            self._browser = self._playwright.chromium.launch(
                headless=False,
                args=[
                    f"--remote-debugging-port={self._port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
            self._connected = True
            logger.info("Playwright launched new Chromium instance")
        except Exception as e:
            logger.error(f"Playwright browser launch failed: {e}")
            raise ConnectionError(
                f"Could not connect to or launch browser: {e}"
            ) from e

    def ensure_browser(self):
        """Launch Chrome with remote debugging if not already running.

        Returns:
            True if Chrome is running with CDP enabled.
        """
        if self.is_browser_available():
            return True

        chrome_path = None
        for p in CHROME_PATHS:
            if os.path.isfile(p):
                chrome_path = p
                break

        if not chrome_path:
            logger.warning("Chrome not found in standard paths")
            return False

        try:
            subprocess.Popen(
                [
                    chrome_path,
                    f"--remote-debugging-port={self._port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            self._launched_chrome = True
            # Wait for Chrome to start and expose CDP
            for _ in range(10):
                time.sleep(0.5)
                if self.is_browser_available():
                    logger.info("Chrome launched with CDP for Playwright")
                    return True
            logger.warning("Chrome launched but CDP not responding")
            return False
        except Exception as e:
            logger.warning(f"Failed to launch Chrome: {e}")
            return False

    def is_browser_available(self):
        """Check if a browser is reachable via CDP on the configured port."""
        try:
            from urllib.request import urlopen
            resp = urlopen(
                f"http://{self._host}:{self._port}/json/version",
                timeout=2,
            )
            return resp.status == 200
        except Exception:
            return False

    def _ensure_page(self):
        """Ensure we have an active page, reconnecting if needed."""
        if not self._connected or not self._page:
            self.connect()
        try:
            # Quick liveness check
            self._page.title()
        except Exception:
            self._connected = False
            self.connect()

    # =================================================================
    # Navigation
    # =================================================================

    def navigate(self, url, wait=True, timeout=15000):
        """Navigate to a URL with optional wait for page load.

        Args:
            url: The URL to navigate to.
            wait: If True, wait for the page to finish loading.
            timeout: Max wait time in milliseconds.
        """
        with self._lock:
            self._ensure_page()
            if not url.startswith(("http://", "https://", "file://", "about:")):
                url = "https://" + url
            wait_until = "load" if wait else "commit"
            try:
                self._page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as e:
                # Playwright raises on navigation errors but page may still load
                logger.debug(f"Playwright navigate warning: {e}")
            return True

    def get_url(self):
        """Get the current page URL."""
        with self._lock:
            self._ensure_page()
            return self._page.url

    def get_title(self):
        """Get the current page title."""
        with self._lock:
            self._ensure_page()
            return self._page.title()

    def back(self):
        """Navigate back in history."""
        with self._lock:
            self._ensure_page()
            self._page.go_back()

    def forward(self):
        """Navigate forward in history."""
        with self._lock:
            self._ensure_page()
            self._page.go_forward()

    # =================================================================
    # Element interaction
    # =================================================================

    def click(self, selector=None, text=None, timeout=5000):
        """Click an element by CSS selector or visible text.

        Args:
            selector: CSS selector (e.g., '#submit', '.btn-primary').
            text: Visible text to find and click (uses Playwright text selector).
            timeout: Max wait time in milliseconds.

        Returns:
            True if click succeeded.
        """
        with self._lock:
            self._ensure_page()
            if selector:
                self._page.click(selector, timeout=timeout)
            elif text:
                # Playwright's text selector — matches visible text
                self._page.click(f"text={text}", timeout=timeout)
            else:
                raise ValueError("Provide a CSS selector or text to click")
            return True

    def fill(self, selector, text):
        """Fill a form field (clears existing value first).

        Args:
            selector: CSS selector for the input/textarea.
            text: Text to type into the field.
        """
        with self._lock:
            self._ensure_page()
            self._page.fill(selector, text)
            return True

    # =================================================================
    # Reading page content
    # =================================================================

    def get_text(self, selector=None):
        """Get text content of the page or a specific element.

        Args:
            selector: CSS selector (None = entire page body).

        Returns:
            str: Text content (truncated to 5000 chars).
        """
        with self._lock:
            self._ensure_page()
            if selector:
                el = self._page.query_selector(selector)
                text = el.inner_text() if el else ""
            else:
                text = self._page.inner_text("body")
            return text[:5000] if text else ""

    def get_html(self, selector=None):
        """Get HTML of the page or a specific element.

        Args:
            selector: CSS selector (None = full page).

        Returns:
            str: HTML content (truncated to 10000 chars).
        """
        with self._lock:
            self._ensure_page()
            if selector:
                el = self._page.query_selector(selector)
                html = el.evaluate("el => el.outerHTML") if el else ""
            else:
                html = self._page.content()
            return html[:10000] if html else ""

    def run_js(self, expression):
        """Evaluate JavaScript in the page context.

        Args:
            expression: JavaScript code to execute.

        Returns:
            The result of the evaluation.
        """
        with self._lock:
            self._ensure_page()
            return self._page.evaluate(expression)

    def find_text(self, text):
        """Check if text exists on the page.

        Args:
            text: Text to search for.

        Returns:
            True if the text is found.
        """
        with self._lock:
            self._ensure_page()
            try:
                locator = self._page.get_by_text(text, exact=False)
                return locator.count() > 0
            except Exception:
                # Fallback to JS-based search
                escaped = text.replace("'", "\\'")
                return bool(self._page.evaluate(
                    f"document.body?.innerText?.includes('{escaped}') || false"
                ))

    # =================================================================
    # Screenshots
    # =================================================================

    def screenshot(self, path=None):
        """Capture a screenshot of the page.

        Args:
            path: File path to save to. If None, returns base64 string.

        Returns:
            str: File path if saved, or base64-encoded PNG data.
        """
        with self._lock:
            self._ensure_page()
            if path:
                self._page.screenshot(path=path)
                return path
            else:
                data = self._page.screenshot()
                return base64.b64encode(data).decode("utf-8")

    def screenshot_to_file(self, path, format="png"):
        """Take a screenshot and save to file.

        Args:
            path: File path to save the screenshot.
            format: Image format (only 'png' supported by Playwright).

        Returns:
            str: The file path, or None on failure.
        """
        with self._lock:
            self._ensure_page()
            try:
                self._page.screenshot(path=path)
                return path
            except Exception as e:
                logger.error(f"Playwright screenshot error: {e}")
                return None

    # =================================================================
    # Wait / element presence
    # =================================================================

    def wait_for_element(self, selector, timeout=10000):
        """Wait for an element to appear in the DOM.

        Args:
            selector: CSS selector to wait for.
            timeout: Max wait time in milliseconds.

        Returns:
            True if element found within timeout.

        Raises:
            TimeoutError: If element not found within timeout.
        """
        with self._lock:
            self._ensure_page()
            try:
                self._page.wait_for_selector(selector, timeout=timeout)
                return True
            except Exception as e:
                raise TimeoutError(
                    f"Element {selector} not found within {timeout}ms"
                ) from e

    # =================================================================
    # Tab management
    # =================================================================

    def get_tabs(self):
        """List all open tabs/pages.

        Returns:
            list[dict]: Each dict has 'title', 'url', and 'id'.
        """
        with self._lock:
            self._ensure_page()
            tabs = []
            if self._context:
                for i, page in enumerate(self._context.pages):
                    try:
                        tabs.append({
                            "title": page.title(),
                            "url": page.url,
                            "id": str(i),
                        })
                    except Exception:
                        tabs.append({
                            "title": "(closed)",
                            "url": "",
                            "id": str(i),
                        })
            return tabs

    def switch_tab(self, index):
        """Switch to a tab by index.

        Args:
            index: 0-based tab index.
        """
        with self._lock:
            self._ensure_page()
            if self._context:
                pages = self._context.pages
                if 0 <= index < len(pages):
                    self._page = pages[index]
                    self._page.bring_to_front()
                    return True
            raise ValueError(f"Tab index {index} out of range")

    def new_tab(self, url="about:blank"):
        """Open a new tab and navigate to URL.

        Args:
            url: URL to open in the new tab.

        Returns:
            str: Index of the new tab.
        """
        with self._lock:
            self._ensure_page()
            if self._context:
                new_page = self._context.new_page()
                if url and url != "about:blank":
                    try:
                        new_page.goto(url, wait_until="load", timeout=15000)
                    except Exception as e:
                        logger.debug(f"New tab navigate warning: {e}")
                self._page = new_page
                return str(len(self._context.pages) - 1)
            raise ConnectionError("No browser context available")

    def close_tab(self, tab_id=None):
        """Close a tab. Closes current tab if no ID given.

        Args:
            tab_id: String index of the tab to close.
        """
        with self._lock:
            self._ensure_page()
            if tab_id is not None and self._context:
                pages = self._context.pages
                idx = int(tab_id)
                if 0 <= idx < len(pages):
                    pages[idx].close()
                    # Switch to another page if we closed the current one
                    if pages[idx] == self._page:
                        remaining = self._context.pages
                        self._page = remaining[0] if remaining else None
                    return
            # Close current page
            if self._page:
                self._page.close()
                if self._context:
                    remaining = self._context.pages
                    self._page = remaining[0] if remaining else None

    # =================================================================
    # Cleanup
    # =================================================================

    def close(self):
        """Close the Playwright session and release resources."""
        with self._lock:
            self._connected = False
            try:
                if self._browser:
                    self._browser.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# =====================================================================
# Module-level singleton
# =====================================================================

_session = None
_session_lock = threading.Lock()


def get_playwright_session():
    """Get or create the global Playwright session (thread-safe singleton).

    Returns:
        PlaywrightSession instance.

    Raises:
        ImportError: If playwright is not installed.
    """
    global _session
    with _session_lock:
        if _session is None or not _session._connected:
            _session = PlaywrightSession()
        return _session
