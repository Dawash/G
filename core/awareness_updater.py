"""
Awareness Updater — subscribes to events and runs background monitors to keep
AwarenessState continuously up to date.

Call once at startup:
    from core.awareness_updater import start_awareness_updates
    start_awareness_updates()
"""
from __future__ import annotations

import platform
import re
import threading
import time
from datetime import datetime
from typing import Tuple

from core.awareness_state import awareness
from core.event_bus import bus
from core.timeouts import Timeouts
from core.topics import Topics


def start_awareness_updates() -> None:
    """Subscribe to all relevant events and start all background perception threads."""

    # --- Event bus subscriptions ---

    @bus.on(Topics.SPEECH_RECOGNIZED)
    def _on_speech(event: object) -> None:
        text = event.payload.get("text", "")
        if text:
            cmds = list(awareness.recent_commands[-9:])
            cmds.append(text)
            awareness.update(recent_commands=cmds, last_interaction_ago=0, user_present=True)

    @bus.on(Topics.INPUT_RECEIVED)
    def _on_input(event: object) -> None:
        text = event.payload.get("text", "")
        if text:
            cmds = list(awareness.recent_commands[-9:])
            cmds.append(text)
            awareness.update(recent_commands=cmds, last_interaction_ago=0, user_present=True)

    @bus.on(Topics.STATE_ACTIVE)
    def _on_active(event: object) -> None:
        awareness.update(user_present=True, last_interaction_ago=0)

    @bus.on(Topics.STATE_IDLE)
    def _on_idle(event: object) -> None:
        awareness.update(user_present=False)

    @bus.on(Topics.RESPONSE_READY)
    def _on_response(event: object) -> None:
        # Response text can hint at conversation topic — update last_interaction_ago reset
        awareness.update(last_interaction_ago=0)

    # --- Background threads ---
    _start_daemon("awareness-time", _time_updater)
    _start_daemon("awareness-window", _window_tracker)
    _start_daemon("awareness-system", _system_monitor)
    _start_daemon("awareness-timer", _interaction_timer)
    _start_daemon("awareness-clipboard", _clipboard_monitor)
    _start_daemon("awareness-publisher", _awareness_publisher)

    # Run time updater immediately so state is populated before first LLM call
    try:
        _update_time_now()
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).debug("awareness initial time update failed: %s", e)


# ---------------------------------------------------------------------------
# Daemon thread launcher
# ---------------------------------------------------------------------------

def _start_daemon(name: str, target) -> threading.Thread:
    """Start a daemon background thread."""
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Background perception loops
# ---------------------------------------------------------------------------

def _update_time_now() -> None:
    """Update time fields immediately (called once on startup)."""
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        tod = "night"
    elif hour < 12:
        tod = "morning"
    elif hour < 17:
        tod = "afternoon"
    elif hour < 21:
        tod = "evening"
    else:
        tod = "night"

    day = now.strftime("%A")
    day_type = "weekend" if day in ("Saturday", "Sunday") else "workday"
    awareness.update(
        current_time=now.strftime("%H:%M"),
        current_date=now.strftime("%Y-%m-%d"),
        time_of_day=tod,
        day_type=day_type,
    )


def _time_updater() -> None:
    """Update time and day-type fields every 30 seconds."""
    while True:
        try:
            _update_time_now()
        except Exception:
            pass
        time.sleep(Timeouts.AWARENESS_TIME_POLL)


def _window_tracker() -> None:
    """Track the active foreground window every 3 seconds."""
    while True:
        try:
            app, title, filename = _get_active_window_info()
            activity = _classify_activity(app, title)
            updates: dict = {"activity": activity}
            if app:
                updates["active_app"] = app
            if title:
                updates["active_window_title"] = title
            if filename:
                updates["active_file"] = filename
            awareness.update(**updates)
        except Exception:
            pass
        time.sleep(Timeouts.AWARENESS_WINDOW_POLL)


def _system_monitor() -> None:
    """Poll CPU, RAM, battery, disk every 10 seconds."""
    while True:
        try:
            stats = _get_system_stats()
            if stats:
                awareness.update(**stats)
        except Exception:
            pass
        time.sleep(Timeouts.AWARENESS_SYSTEM_POLL)


def _interaction_timer() -> None:
    """Increment last_interaction_ago by 5 every 5 seconds."""
    while True:
        time.sleep(5)
        try:
            current = awareness.last_interaction_ago
            awareness.update(last_interaction_ago=current + 5)
        except Exception:
            pass


def _clipboard_monitor() -> None:
    """Monitor clipboard changes every 5 seconds and update awareness."""
    last_clip = ""
    system = platform.system()
    while True:
        try:
            clip = _read_clipboard(system)
            if clip and clip != last_clip:
                last_clip = clip
                awareness.update(clipboard_preview=clip[:200])
        except Exception:
            pass
        time.sleep(Timeouts.AWARENESS_CLIPBOARD_POLL)


def _awareness_publisher() -> None:
    """Publish an awareness snapshot to the event bus every 5 seconds."""
    while True:
        try:
            bus.publish(Topics.CONTEXT_UPDATE, awareness.snapshot(), source="awareness_updater")
        except Exception:
            pass
        time.sleep(Timeouts.AWARENESS_CLIPBOARD_POLL)


# ---------------------------------------------------------------------------
# Window detection helpers
# ---------------------------------------------------------------------------

def _get_active_window_info() -> Tuple[str, str, str]:
    """Get foreground window: (app_name, window_title, filename).

    Uses platform-appropriate method. Returns ("", "", "") on failure.
    """
    app = ""
    title = ""

    system = platform.system()
    if system == "Windows":
        app, title = _get_active_window_windows()
    elif system == "Linux":
        app, title = _get_active_window_linux()
    elif system == "Darwin":
        app, title = _get_active_window_macos()

    filename = _extract_filename(title)
    return app, title, filename


def _get_active_window_windows() -> Tuple[str, str]:
    """Get active window on Windows using ctypes + optional psutil."""
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        app = ""
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            import psutil
            proc = psutil.Process(pid.value)
            app = proc.name().replace(".exe", "")
        except Exception:
            pass

        return app, title
    except Exception:
        return "", ""


def _get_active_window_linux() -> Tuple[str, str]:
    """Get active window on Linux using xdotool."""
    try:
        import subprocess
        r1 = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=2,
            encoding="utf-8", errors="replace"
        )
        title = r1.stdout.strip()

        app = ""
        try:
            r2 = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowpid"],
                capture_output=True, text=True, timeout=2,
                encoding="utf-8", errors="replace"
            )
            pid = r2.stdout.strip()
            if pid:
                import psutil
                app = psutil.Process(int(pid)).name()
        except Exception:
            pass

        return app, title
    except Exception:
        return "", ""


def _get_active_window_macos() -> Tuple[str, str]:
    """Get active window on macOS using AppleScript."""
    try:
        import subprocess
        script = (
            'tell application "System Events"\n'
            '  set p to first application process whose frontmost is true\n'
            '  set n to name of p\n'
            '  try\n'
            '    set w to name of front window of p\n'
            '  on error\n'
            '    set w to ""\n'
            '  end try\n'
            'end tell\n'
            'return n & "|" & w'
        )
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=3,
                           encoding="utf-8", errors="replace")
        parts = r.stdout.strip().split("|", 1)
        app = parts[0] if parts else ""
        title = parts[1] if len(parts) > 1 else ""
        return app, title
    except Exception:
        return "", ""


def _extract_filename(title: str) -> str:
    """Extract a filename from a window title if one is present.

    Handles common patterns like:
      "main.py - Visual Studio Code"
      "document.docx - Word"
      "~/projects/app/src/index.ts — Sublime Text"
    """
    if not title:
        return ""
    match = re.match(r"^(.+?\.\w{1,10})\s*[-\u2013\u2014]", title)
    if match:
        path = match.group(1).strip()
        return path.split("/")[-1].split("\\")[-1]
    return ""


def _classify_activity(app: str, title: str) -> str:
    """Classify user activity from app name and window title.

    Returns one of: coding/browsing/gaming/reading/writing/
                    video-call/communication/media/idle
    """
    a = app.lower()
    t = title.lower()

    _CODING = {"code", "vscode", "pycharm", "intellij", "sublime", "vim", "nvim",
               "neovim", "atom", "notepad++", "visual studio", "cursor", "zed",
               "terminal", "cmd", "powershell", "windowsterminal", "wt", "iterm",
               "alacritty", "kitty", "warp"}
    if any(c in a for c in _CODING):
        return "coding"

    _VIDEO = {"zoom", "teams", "meet", "webex", "skype", "discord"}
    if any(v in a or v in t for v in _VIDEO):
        if any(kw in t for kw in ("meeting", "call", "video", "conference")):
            return "video-call"

    _COMM = {"slack", "discord", "telegram", "whatsapp", "signal", "messages"}
    if any(c in a for c in _COMM):
        return "communication"

    _GAME = {"steam", "epic games", "unity", "unreal"}
    if any(g in a or g in t for g in _GAME):
        return "gaming"

    _BROWSER = {"chrome", "firefox", "edge", "safari", "brave", "opera", "vivaldi", "arc"}
    if any(b in a for b in _BROWSER):
        if any(kw in t for kw in ("docs.google", "notion", "word online", "overleaf")):
            return "writing"
        if any(kw in t for kw in ("github.com", "gitlab", "stackoverflow", "codepen")):
            return "coding"
        if any(kw in t for kw in ("youtube", "netflix", "twitch", "spotify", "prime video")):
            return "media"
        return "browsing"

    _WRITE = {"word", "notion", "obsidian", "typora", "scrivener", "pages", "writer"}
    if any(w in a for w in _WRITE):
        return "writing"

    _READ = {"kindle", "reader", "calibre", "zathura", "evince", "preview", "okular"}
    if any(r in a for r in _READ):
        return "reading"

    return "idle"


def _read_clipboard(system: str) -> str:
    """Read current clipboard text. Returns empty string on failure."""
    try:
        if system == "Windows":
            import subprocess
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="replace"
            )
            return r.stdout.strip()[:200]
        elif system == "Linux":
            import subprocess
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True, timeout=2,
                encoding="utf-8", errors="replace"
            )
            return r.stdout.strip()[:200]
        elif system == "Darwin":
            import subprocess
            r = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=2,
                encoding="utf-8", errors="replace"
            )
            return r.stdout.strip()[:200]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# System stats helper
# ---------------------------------------------------------------------------

def _get_system_stats() -> dict:
    """Collect CPU, RAM, battery, disk stats via psutil.

    Returns a dict suitable for awareness.update(**stats).
    Returns empty dict if psutil unavailable.
    """
    stats: dict = {}
    try:
        import psutil

        stats["cpu_percent"] = psutil.cpu_percent(interval=None)
        stats["ram_percent"] = psutil.virtual_memory().percent

        bat = psutil.sensors_battery()
        if bat is not None:
            stats["battery_percent"] = int(bat.percent)
            stats["battery_charging"] = bool(bat.power_plugged)

        try:
            disk = psutil.disk_usage("/")
            stats["disk_percent"] = disk.percent
        except Exception:
            try:
                disk = psutil.disk_usage("C:\\")
                stats["disk_percent"] = disk.percent
            except Exception:
                pass

        # Derive system health
        cpu = stats.get("cpu_percent", 0)
        ram = stats.get("ram_percent", 0)
        disk_pct = stats.get("disk_percent", 0)
        bat_pct = stats.get("battery_percent", 100)

        if cpu > 90 or ram > 95 or disk_pct > 95 or bat_pct < 5:
            stats["system_health"] = "critical"
        elif cpu > 75 or ram > 85 or disk_pct > 90 or bat_pct < 15:
            stats["system_health"] = "degraded"
        else:
            stats["system_health"] = "good"

    except ImportError:
        pass

    return stats
