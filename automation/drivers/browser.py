"""
Browser driver — structured operations for Chrome/Edge/Firefox.

Knows how to: navigate, search, open/close tabs, bookmark, zoom,
developer tools, find on page, download, print, etc.
"""

from automation.drivers.base import AppDriver, AppAction, register_driver


class BrowserDriver(AppDriver):
    """Driver for web browsers (Chrome, Edge, Firefox, Brave)."""

    app_name = "Browser"
    process_names = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"]
    window_patterns = ["Chrome", "Edge", "Firefox", "Brave", "Opera", "Vivaldi"]

    def __init__(self):
        super().__init__()

        # --- Keyboard shortcuts ---
        self._register_shortcut("new_tab", "ctrl+t")
        self._register_shortcut("close_tab", "ctrl+w")
        self._register_shortcut("reopen_tab", "ctrl+shift+t")
        self._register_shortcut("next_tab", "ctrl+tab")
        self._register_shortcut("prev_tab", "ctrl+shift+tab")
        self._register_shortcut("address_bar", "ctrl+l")
        self._register_shortcut("find", "ctrl+f")
        self._register_shortcut("refresh", "f5")
        self._register_shortcut("hard_refresh", "ctrl+shift+r")
        self._register_shortcut("back", "alt+left")
        self._register_shortcut("forward", "alt+right")
        self._register_shortcut("bookmark", "ctrl+d")
        self._register_shortcut("history", "ctrl+h")
        self._register_shortcut("downloads", "ctrl+j")
        self._register_shortcut("dev_tools", "f12")
        self._register_shortcut("zoom_in", "ctrl+=")
        self._register_shortcut("zoom_out", "ctrl+-")
        self._register_shortcut("zoom_reset", "ctrl+0")
        self._register_shortcut("print", "ctrl+p")
        self._register_shortcut("save", "ctrl+s")
        self._register_shortcut("fullscreen", "f11")
        self._register_shortcut("close_window", "ctrl+shift+w")

        # --- Registered actions ---
        self._register_action(AppAction(
            name="navigate",
            description="Navigate to a URL",
            steps=[
                ("press_key", {"keys": "ctrl+l"}),
                ("wait", {"seconds": 0.2}),
                ("press_key", {"keys": "ctrl+a"}),
            ],
            preconditions=["browser window is active"],
        ))

        self._register_action(AppAction(
            name="search",
            description="Search the web",
            steps=[
                ("press_key", {"keys": "ctrl+l"}),
                ("wait", {"seconds": 0.2}),
                ("press_key", {"keys": "ctrl+a"}),
            ],
            preconditions=["browser window is active"],
        ))

        self._register_action(AppAction(
            name="find_on_page",
            description="Find text on the current page",
            steps=[("press_key", {"keys": "ctrl+f"})],
            preconditions=["browser window is active"],
        ))

        self._register_action(AppAction(
            name="open_tab",
            description="Open a new tab",
            steps=[("press_key", {"keys": "ctrl+t"})],
        ))

        self._register_action(AppAction(
            name="close_tab",
            description="Close current tab",
            steps=[("press_key", {"keys": "ctrl+w"})],
        ))

        self._register_action(AppAction(
            name="go_back",
            description="Go back to previous page",
            steps=[("press_key", {"keys": "alt+left"})],
        ))

        self._register_action(AppAction(
            name="go_forward",
            description="Go forward to next page",
            steps=[("press_key", {"keys": "alt+right"})],
        ))

        self._register_action(AppAction(
            name="refresh",
            description="Refresh current page",
            steps=[("press_key", {"keys": "f5"})],
        ))

        self._register_action(AppAction(
            name="zoom_in",
            description="Zoom in",
            steps=[("press_key", {"keys": "ctrl+="})],
        ))

        self._register_action(AppAction(
            name="zoom_out",
            description="Zoom out",
            steps=[("press_key", {"keys": "ctrl+-"})],
        ))

    def execute_action(self, action_name, **kwargs):
        """Execute browser action with CDP fallback."""
        # For navigate and search, try CDP first
        if action_name == "navigate" and kwargs.get("url"):
            try:
                from automation.browser_driver import browser_navigate
                return browser_navigate(kwargs["url"])
            except Exception:
                pass

        if action_name == "search" and kwargs.get("query"):
            url = f"https://www.google.com/search?q={kwargs['query']}"
            try:
                from automation.browser_driver import browser_navigate
                return browser_navigate(url)
            except Exception:
                pass

        if action_name == "find_on_page" and kwargs.get("text"):
            try:
                from automation.browser_driver import browser_find_text
                return browser_find_text(kwargs["text"])
            except Exception:
                pass

        # Default step execution
        return super().execute_action(action_name, **kwargs)


# Auto-register
register_driver(BrowserDriver())
