"""
Event-driven task completion — wait for actual events instead of timeouts.

Replaces time.sleep(N) with actual event detection:
  - Window appeared/disappeared
  - Process started/stopped
  - File created/modified
  - URL changed in browser
  - UI element appeared

Each waiter polls at 200ms intervals with a max_wait cap (not a timeout —
the action already succeeded or failed, we just wait for evidence).
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# Default max wait (seconds) — this is NOT a timeout, it's a safety cap
DEFAULT_MAX_WAIT = 15


def wait_for_window(title_contains, max_wait=DEFAULT_MAX_WAIT, interval=0.2):
    """Wait for a window with matching title to appear.

    Args:
        title_contains: Substring to match in window title (case-insensitive)
        max_wait: Maximum seconds to wait
        interval: Poll interval in seconds

    Returns:
        dict: {found: bool, title: str, elapsed: float}
    """
    import pygetwindow as gw
    title_lower = title_contains.lower()
    start = time.time()

    while time.time() - start < max_wait:
        try:
            for w in gw.getAllWindows():
                if w.title and title_lower in w.title.lower():
                    return {"found": True, "title": w.title,
                            "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"found": False, "title": "", "elapsed": time.time() - start}


def wait_for_window_gone(title_contains, max_wait=DEFAULT_MAX_WAIT, interval=0.2):
    """Wait for a window to disappear (closed/minimized).

    Returns:
        dict: {gone: bool, elapsed: float}
    """
    import pygetwindow as gw
    title_lower = title_contains.lower()
    start = time.time()

    while time.time() - start < max_wait:
        try:
            found = False
            for w in gw.getAllWindows():
                if w.title and title_lower in w.title.lower():
                    found = True
                    break
            if not found:
                return {"gone": True, "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"gone": False, "elapsed": time.time() - start}


def wait_for_process(process_name, max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for a process to start running.

    Args:
        process_name: Process name (e.g. "notepad.exe", "chrome")

    Returns:
        dict: {found: bool, pid: int, elapsed: float}
    """
    import psutil
    name_lower = process_name.lower().replace(".exe", "")
    start = time.time()

    while time.time() - start < max_wait:
        try:
            for proc in psutil.process_iter(['name', 'pid']):
                pname = (proc.info['name'] or "").lower().replace(".exe", "")
                if name_lower in pname or pname in name_lower:
                    return {"found": True, "pid": proc.info['pid'],
                            "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"found": False, "pid": 0, "elapsed": time.time() - start}


def wait_for_process_gone(process_name, max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for a process to stop running.

    Returns:
        dict: {gone: bool, elapsed: float}
    """
    import psutil
    name_lower = process_name.lower().replace(".exe", "")
    start = time.time()

    while time.time() - start < max_wait:
        try:
            found = False
            for proc in psutil.process_iter(['name']):
                pname = (proc.info['name'] or "").lower().replace(".exe", "")
                if name_lower in pname or pname in name_lower:
                    found = True
                    break
            if not found:
                return {"gone": True, "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"gone": False, "elapsed": time.time() - start}


def wait_for_file(path, max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for a file to be created.

    Returns:
        dict: {exists: bool, size: int, elapsed: float}
    """
    start = time.time()

    while time.time() - start < max_wait:
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
                return {"exists": True, "size": size,
                        "elapsed": time.time() - start}
            except OSError:
                pass
        time.sleep(interval)

    return {"exists": False, "size": 0, "elapsed": time.time() - start}


def wait_for_file_gone(path, max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for a file to be deleted.

    Returns:
        dict: {gone: bool, elapsed: float}
    """
    start = time.time()

    while time.time() - start < max_wait:
        if not os.path.exists(path):
            return {"gone": True, "elapsed": time.time() - start}
        time.sleep(interval)

    return {"gone": False, "elapsed": time.time() - start}


def wait_for_ui_element(name=None, role=None, window=None,
                        max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for a UI element to appear in the accessibility tree.

    Args:
        name: Element name/text to find
        role: Control type (e.g. "Button", "Edit")
        window: Window title to search in

    Returns:
        dict: {found: bool, element: dict or None, elapsed: float}
    """
    start = time.time()

    while time.time() - start < max_wait:
        try:
            from automation.ui_control import find_control
            ctrl = find_control(name=name, role=role, window=window)
            if ctrl:
                return {"found": True, "element": ctrl,
                        "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"found": False, "element": None, "elapsed": time.time() - start}


def wait_for_browser_url(url_contains, max_wait=DEFAULT_MAX_WAIT, interval=0.3):
    """Wait for browser to navigate to a URL containing the given string.

    Returns:
        dict: {found: bool, url: str, elapsed: float}
    """
    start = time.time()

    while time.time() - start < max_wait:
        try:
            from automation.browser_driver import browser_get_url
            url = browser_get_url()
            if url and url_contains.lower() in url.lower():
                return {"found": True, "url": url,
                        "elapsed": time.time() - start}
        except Exception:
            pass
        time.sleep(interval)

    return {"found": False, "url": "", "elapsed": time.time() - start}


def wait_for_idle(max_wait=5, cpu_threshold=20, interval=0.5):
    """Wait for system to become idle (CPU usage drops below threshold).

    Useful after launching heavy apps that need loading time.

    Returns:
        dict: {idle: bool, cpu_percent: float, elapsed: float}
    """
    import psutil
    start = time.time()

    # Skip first reading (always inaccurate)
    psutil.cpu_percent(interval=0.1)

    while time.time() - start < max_wait:
        cpu = psutil.cpu_percent(interval=interval)
        if cpu < cpu_threshold:
            return {"idle": True, "cpu_percent": cpu,
                    "elapsed": time.time() - start}

    cpu = psutil.cpu_percent(interval=0.1)
    return {"idle": False, "cpu_percent": cpu, "elapsed": time.time() - start}
