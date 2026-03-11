"""
Post-tool completion verification.

Checks if a tool action actually completed (not just started).
Uses process checks (tasklist) and window title inspection.
Supports auto-escalation to desktop agent for partial completions.
"""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

# Tools that need post-execution verification
VERIFY_TOOLS = {"play_music", "search_in_app", "open_app", "google_search"}

# App name → (exe_name, window_title_keyword) for process/window checks
APP_VERIFY = {
    "spotify": ("Spotify.exe", "Spotify"),
    "chrome": ("chrome.exe", "Chrome"),
    "firefox": ("firefox.exe", "Firefox"),
    "edge": ("msedge.exe", "Edge"),
    "steam": ("steam.exe", "Steam"),
    "discord": ("Discord.exe", "Discord"),
    "notepad": ("notepad.exe", "Notepad"),
    "code": ("Code.exe", "Visual Studio Code"),
    "vs code": ("Code.exe", "Visual Studio Code"),
    "youtube": (None, "YouTube"),
    "google": (None, "Google"),
}


def verify_tool_completion(tool_name, arguments, result, user_input=""):
    """Check if a tool action actually completed, not just started.

    Returns:
        tuple: (is_complete, what_done, what_missing)
    """
    if tool_name not in VERIFY_TOOLS:
        return True, [], []

    if tool_name == "play_music":
        action = arguments.get("action", "play")
        if action in ("pause", "next", "previous", "volume_up", "volume_down", "mute"):
            return True, [], []
        result_lower = str(result).lower()
        if any(w in result_lower for w in ["playing", "searched for", "play first result", "pressed enter"]):
            return True, ["music action performed"], []

    app = ""
    query = ""
    if tool_name == "play_music":
        app = arguments.get("app", "spotify").lower()
        query = arguments.get("query", "")
    elif tool_name == "search_in_app":
        app = arguments.get("app", "").lower()
        query = arguments.get("query", "")
    elif tool_name == "open_app":
        app = arguments.get("name", "").lower()
    elif tool_name == "google_search":
        query = arguments.get("query", "")
        app = "chrome"

    if not app and not query:
        return True, [], []

    what_done = []
    what_missing = []

    time.sleep(0.5)

    app_info = APP_VERIFY.get(app)
    if app_info and app_info[0]:
        exe = app_info[0]
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/V", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10)
            if exe.lower() in proc.stdout.lower():
                what_done.append(f"{app} is running")

                if query:
                    import csv as _csv
                    import io as _io
                    q_lower = query.lower()
                    reader = _csv.reader(_io.StringIO(proc.stdout.strip()))
                    next(reader, None)
                    for row in reader:
                        if len(row) >= 9:
                            title = row[-1].strip().lower()
                            if q_lower in title:
                                what_done.append(f"'{query}' found in window title")
                                return True, what_done, []
                    what_missing.append(f"'{query}' not in {app} window title")
                else:
                    return True, what_done, []
            else:
                what_missing.append(f"{app} not running")
        except Exception as e:
            logger.debug(f"Verify process check failed: {e}")

    if query:
        try:
            import pygetwindow as gw
            titles = gw.getAllTitles()
            q_lower = query.lower()
            for title in titles:
                if title and q_lower in title.lower():
                    what_done.append(f"'{query}' found in window: {title[:50]}")
                    return True, what_done, []
        except Exception:
            pass

        if app_info and app_info[1]:
            try:
                import pygetwindow as gw
                kw = app_info[1].lower()
                for title in gw.getAllTitles():
                    if title and kw in title.lower():
                        what_done.append(f"{app} window open: {title[:50]}")
                        break
            except Exception:
                pass

        if not what_done:
            what_missing.append(f"no window with '{query}' found")

    if what_missing and not what_done:
        return False, what_done, what_missing
    if what_missing:
        return False, what_done, what_missing
    return True, what_done, []
