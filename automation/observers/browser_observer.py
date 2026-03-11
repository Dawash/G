"""Browser state observer — structured state via CDP, no side effects.

Reads browser tabs, current URL, page structure, page text via Chrome
DevTools Protocol. Falls back to UIA (address bar) and window title.
Sub-second execution for CDP calls.
"""

import logging
from dataclasses import dataclass, field

from automation.observers.base import ObservationResult

logger = logging.getLogger(__name__)


@dataclass
class TabInfo:
    """Structured browser tab state."""
    title: str = ""
    url: str = ""
    index: int = 0
    is_active: bool = False
    ws_url: str = ""  # WebSocket debugger URL (internal)


@dataclass
class PageSnapshot:
    """Structured page content — replaces screenshot + vision."""
    url: str = ""
    title: str = ""
    text: str = ""     # Main content text (truncated)
    links: list = field(default_factory=list)    # [{text, href}]
    inputs: list = field(default_factory=list)   # [{type, name, value}]
    buttons: list = field(default_factory=list)  # [{text}]


class BrowserObserver:
    """Reads browser state via CDP with UIA/title fallback. No side effects."""

    def is_browser_running(self):
        """Check if any browser window is open."""
        try:
            from automation.browser_driver import is_browser_active
            return is_browser_active()
        except Exception:
            return False

    def is_cdp_available(self):
        """Check if Chrome DevTools Protocol is accessible."""
        try:
            from automation.browser_driver import is_cdp_available
            return is_cdp_available()
        except Exception:
            return False

    def get_current_url(self):
        """Get the URL of the active browser tab.

        Tier 1: CDP /json endpoint
        Tier 2: UIA address bar
        Tier 3: Window title parsing

        Returns:
            str: URL or empty string
        """
        try:
            from automation.browser_driver import browser_get_url
            url = browser_get_url()
            if url:
                return url
        except Exception:
            pass
        return ""

    def get_current_title(self):
        """Get the title of the active browser tab.

        Returns:
            str: Page title or empty string
        """
        # Try CDP
        try:
            from automation.browser_driver import _get_tabs, _check_cdp
            if _check_cdp():
                tabs = _get_tabs()
                if tabs:
                    return tabs[0].get("title", "")
        except Exception:
            pass

        # Fallback: window title
        try:
            from automation.ui_control import get_active_window_info
            info = get_active_window_info()
            if info:
                title = info.get("title", "")
                # Browser titles end with " - Chrome", " - Edge", etc.
                for suffix in (" - Google Chrome", " - Microsoft Edge",
                              " - Firefox", " - Brave", " - Opera"):
                    if title.endswith(suffix):
                        return title[:-len(suffix)]
                return title
        except Exception:
            pass
        return ""

    def get_all_tabs(self):
        """List all open browser tabs.

        Returns:
            list[TabInfo]
        """
        try:
            from automation.browser_driver import browser_get_tabs
            raw_tabs = browser_get_tabs()
            return [
                TabInfo(
                    title=t.get("title", ""),
                    url=t.get("url", ""),
                    index=i,
                    is_active=(i == 0),
                )
                for i, t in enumerate(raw_tabs)
            ]
        except Exception:
            return []

    def get_tab_count(self):
        """Get the number of open tabs."""
        return len(self.get_all_tabs())

    def get_page_snapshot(self):
        """Get structured page content (links, inputs, buttons, text).

        Replaces screenshot + vision for understanding page structure.

        Returns:
            PageSnapshot
        """
        try:
            from automation.browser_driver import browser_snapshot
            snap = browser_snapshot()
            return PageSnapshot(
                url=snap.get("url", ""),
                title=snap.get("title", ""),
                links=snap.get("links", []),
                inputs=snap.get("inputs", []),
                buttons=snap.get("buttons", []),
            )
        except Exception:
            return PageSnapshot()

    def read_page_text(self, selector=None):
        """Read text content from the active page.

        Args:
            selector: CSS selector to read (None = main content).

        Returns:
            str: Page text (truncated to 3000 chars)
        """
        try:
            from automation.browser_driver import browser_read
            return browser_read(selector) or ""
        except Exception:
            return ""

    def observe(self):
        """Full browser state snapshot.

        Returns:
            ObservationResult
        """
        is_running = self.is_browser_running()
        cdp = self.is_cdp_available()
        url = self.get_current_url() if is_running else ""
        title = self.get_current_title() if is_running else ""
        tabs = self.get_all_tabs() if is_running else []

        data = {
            "is_running": is_running,
            "cdp_available": cdp,
            "current_url": url,
            "current_title": title,
            "tab_count": len(tabs),
            "tabs": [
                {"title": t.title, "url": t.url, "active": t.is_active}
                for t in tabs[:15]
            ],
        }

        source = "cdp" if cdp else ("uia" if is_running else "none")

        return ObservationResult(
            domain="browser",
            data=data,
            confidence=1.0 if cdp else (0.7 if is_running else 0.5),
            source=source,
            stale_after=3.0,
        )
