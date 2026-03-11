"""
File Explorer driver — structured operations for Windows Explorer.

Knows how to: navigate folders, create/rename/delete files, select items,
copy/paste, change views, access context menu, etc.
"""

from automation.drivers.base import AppDriver, AppAction, register_driver


class ExplorerDriver(AppDriver):
    """Driver for Windows File Explorer."""

    app_name = "File Explorer"
    process_names = ["explorer.exe"]
    window_patterns = ["File Explorer", "Documents", "Downloads", "Desktop",
                       "Pictures", "Videos", "Music", "This PC"]

    def __init__(self):
        super().__init__()

        # --- Keyboard shortcuts ---
        self._register_shortcut("new_folder", "ctrl+shift+n")
        self._register_shortcut("rename", "f2")
        self._register_shortcut("delete", "delete")
        self._register_shortcut("permanent_delete", "shift+delete")
        self._register_shortcut("copy", "ctrl+c")
        self._register_shortcut("paste", "ctrl+v")
        self._register_shortcut("cut", "ctrl+x")
        self._register_shortcut("select_all", "ctrl+a")
        self._register_shortcut("undo", "ctrl+z")
        self._register_shortcut("redo", "ctrl+y")
        self._register_shortcut("properties", "alt+enter")
        self._register_shortcut("address_bar", "ctrl+l")
        self._register_shortcut("search", "ctrl+f")
        self._register_shortcut("refresh", "f5")
        self._register_shortcut("parent_folder", "alt+up")
        self._register_shortcut("back", "alt+left")
        self._register_shortcut("forward", "alt+right")
        self._register_shortcut("preview_pane", "alt+p")
        self._register_shortcut("details_pane", "alt+shift+p")
        self._register_shortcut("new_window", "ctrl+n")
        self._register_shortcut("close", "alt+f4")

        # --- Registered actions ---
        self._register_action(AppAction(
            name="navigate_to",
            description="Navigate to a folder path",
            steps=[
                ("press_key", {"keys": "ctrl+l"}),
                ("wait", {"seconds": 0.2}),
            ],
            preconditions=["explorer window is active"],
        ))

        self._register_action(AppAction(
            name="new_folder",
            description="Create a new folder",
            steps=[("press_key", {"keys": "ctrl+shift+n"})],
            postconditions=["new folder appears with rename active"],
        ))

        self._register_action(AppAction(
            name="select_all",
            description="Select all items in current folder",
            steps=[("press_key", {"keys": "ctrl+a"})],
        ))

        self._register_action(AppAction(
            name="copy_selected",
            description="Copy selected items",
            steps=[("press_key", {"keys": "ctrl+c"})],
        ))

        self._register_action(AppAction(
            name="paste",
            description="Paste copied items",
            steps=[("press_key", {"keys": "ctrl+v"})],
        ))

        self._register_action(AppAction(
            name="rename",
            description="Rename selected item",
            steps=[("press_key", {"keys": "f2"})],
        ))

        self._register_action(AppAction(
            name="delete",
            description="Delete selected items (to Recycle Bin)",
            steps=[("press_key", {"keys": "delete"})],
        ))

    def execute_action(self, action_name, **kwargs):
        """Execute Explorer action with path navigation support."""
        if action_name == "navigate_to" and kwargs.get("path"):
            import os
            path = kwargs["path"]
            # Resolve common names
            home = os.path.expanduser("~")
            shortcuts = {
                "desktop": os.path.join(home, "Desktop"),
                "documents": os.path.join(home, "Documents"),
                "downloads": os.path.join(home, "Downloads"),
                "pictures": os.path.join(home, "Pictures"),
                "videos": os.path.join(home, "Videos"),
                "music": os.path.join(home, "Music"),
            }
            path = shortcuts.get(path.lower(), path)

            try:
                from automation.ui_control import focus_window
                focus_window("File Explorer")
                import time
                time.sleep(0.3)
                import pyautogui
                pyautogui.hotkey("ctrl", "l")
                time.sleep(0.2)
                import pyperclip
                pyperclip.copy(path)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.1)
                pyautogui.press("enter")
                return f"Navigated to {path}"
            except Exception as e:
                return f"Navigation failed: {e}"

        return super().execute_action(action_name, **kwargs)


# Auto-register
register_driver(ExplorerDriver())
