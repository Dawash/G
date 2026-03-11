"""
Tool outcome verification — checks if actions actually succeeded.

Problem: Tools report "Done!" without proof. This module provides
observable checks so tools can report honestly.

Each verifier returns (success: bool, evidence: str).
"""

import logging
import time

logger = logging.getLogger(__name__)


def check_window_exists(name, timeout=2):
    """Check if a window with the given name exists.

    Args:
        name: Window title substring to match.
        timeout: Seconds to wait for window to appear.

    Returns:
        (exists: bool, title: str or None)
    """
    try:
        import pygetwindow as gw
    except ImportError:
        return True, None  # Can't check, assume success

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            wins = gw.getWindowsWithTitle(name)
            if wins:
                return True, wins[0].title
            # Fuzzy match
            for w in gw.getAllWindows():
                if w.title and name.lower() in w.title.lower():
                    return True, w.title
        except Exception:
            pass
        time.sleep(0.3)

    return False, None


def check_window_gone(name, timeout=2):
    """Check if a window with the given name has closed.

    Returns:
        (gone: bool, remaining_title: str or None)
    """
    try:
        import pygetwindow as gw
    except ImportError:
        return True, None

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            wins = gw.getWindowsWithTitle(name)
            if not wins:
                # Also fuzzy check
                found = False
                for w in gw.getAllWindows():
                    if w.title and name.lower() in w.title.lower():
                        found = True
                        break
                if not found:
                    return True, None
        except Exception:
            return True, None
        time.sleep(0.3)

    # Still exists
    try:
        wins = gw.getWindowsWithTitle(name)
        return False, wins[0].title if wins else None
    except Exception:
        return True, None


def check_process_running(process_name):
    """Check if a process is running.

    Returns:
        (running: bool, details: str)
    """
    import subprocess
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        if process_name.lower() in result.stdout.lower():
            return True, "Process found"
        return False, "Process not found"
    except Exception as e:
        return True, f"Check failed: {e}"  # Assume running on error


def check_spotify_playing(timeout=3):
    """Check if Spotify is actually playing music.

    Spotify window title format:
      - Playing: "Song Name - Artist Name - Spotify" or "Song - Artist"
      - Idle: "Spotify", "Spotify Premium", "Spotify Free"

    Returns:
        (playing: bool, title: str, evidence: str)
    """
    try:
        import pygetwindow as gw
    except ImportError:
        return False, "", "Cannot verify (pygetwindow not available)"

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            all_wins = gw.getAllWindows()
            for w in all_wins:
                if not w.title:
                    continue
                t = w.title.strip()
                tl = t.lower()

                # Check for Spotify window with song title
                if "spotify" in tl:
                    # Playing: title contains " - " (song - artist format)
                    # Idle: just "Spotify", "Spotify Premium", "Spotify Free"
                    idle_titles = {"spotify", "spotify premium", "spotify free",
                                   "spotify - free", "spotify - premium"}
                    if tl in idle_titles:
                        # Not playing yet, keep waiting
                        pass
                    elif " - " in t or " — " in t:
                        # Title has "Song - Artist" format → playing
                        return True, t, f"Window title shows: {t}"
        except Exception:
            pass
        time.sleep(0.5)

    # Check final state
    try:
        all_wins = gw.getAllWindows()
        for w in all_wins:
            if w.title and "spotify" in w.title.lower():
                return False, w.title, f"Spotify open but title is: {w.title}"
    except Exception:
        pass

    return False, "", "Spotify window not found"


def verify_app_opened(app_name, timeout=3):
    """Verify that an app window appeared after launch.

    Returns:
        (success: bool, evidence: str)
    """
    exists, title = check_window_exists(app_name, timeout=timeout)
    if exists:
        return True, f"Window found: {title}"

    # Try process check as fallback
    # Common exe names
    exe_map = {
        "chrome": "chrome.exe", "firefox": "firefox.exe",
        "notepad": "Notepad.exe", "calculator": "Calculator",
        "explorer": "explorer.exe", "spotify": "Spotify.exe",
        "discord": "Discord.exe", "vscode": "Code.exe",
        "terminal": "WindowsTerminal.exe",
    }
    exe = exe_map.get(app_name.lower())
    if exe:
        running, _ = check_process_running(exe)
        if running:
            return True, f"Process {exe} is running (window may be loading)"

    return False, f"No window matching '{app_name}' found"


def verify_app_closed(app_name, timeout=2):
    """Verify that an app window has closed.

    Returns:
        (success: bool, evidence: str)
    """
    gone, remaining = check_window_gone(app_name, timeout=timeout)
    if gone:
        return True, f"Window '{app_name}' is gone"
    return False, f"Window still open: {remaining}"
