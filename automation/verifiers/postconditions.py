"""Library of typed postcondition checks.

Every function returns bool. No mutations. No LLM calls.
Sub-100ms execution. Uses observers for state reading.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy-initialized observers (shared across checks)
_windows_obs = None
_browser_obs = None
_fs_obs = None


def _get_windows():
    global _windows_obs
    if _windows_obs is None:
        from automation.observers.windows_observer import WindowsObserver
        _windows_obs = WindowsObserver()
    return _windows_obs


def _get_browser():
    global _browser_obs
    if _browser_obs is None:
        from automation.observers.browser_observer import BrowserObserver
        _browser_obs = BrowserObserver()
    return _browser_obs


def _get_fs():
    global _fs_obs
    if _fs_obs is None:
        from automation.observers.filesystem_observer import FilesystemObserver
        _fs_obs = FilesystemObserver()
    return _fs_obs


# ===================================================================
# Verification dispatch
# ===================================================================

def verify(conditions):
    """Run a list of postcondition strings and return (all_passed, details).

    Each condition is a string like "url_is:https://google.com" or
    "window_exists:Chrome" or "file_exists:Desktop/report.pdf".

    Args:
        conditions: List of condition strings.

    Returns:
        (bool, list[dict]): (all_passed, [{condition, passed, error}])
    """
    if not conditions:
        return True, []

    results = []
    all_ok = True
    for cond in conditions:
        parts = cond.split(":", 1)
        check_name = parts[0]
        check_arg = parts[1] if len(parts) > 1 else ""

        checker = _CHECKS.get(check_name)
        if not checker:
            results.append({"condition": cond, "passed": False,
                           "error": f"Unknown check: {check_name}"})
            all_ok = False
            continue

        try:
            passed = checker(check_arg) if check_arg else checker()
            results.append({"condition": cond, "passed": passed, "error": None})
            if not passed:
                all_ok = False
        except Exception as e:
            results.append({"condition": cond, "passed": False, "error": str(e)})
            all_ok = False

    return all_ok, results


# ===================================================================
# Individual checks
# ===================================================================

def url_is(expected):
    """Check if current browser URL matches exactly."""
    current = _get_browser().get_current_url()
    return current.rstrip("/") == expected.rstrip("/")


def url_contains(fragment):
    """Check if current browser URL contains a fragment."""
    current = _get_browser().get_current_url()
    return fragment.lower() in current.lower()


def window_is_focused(name):
    """Check if a window with the given name is currently focused."""
    active = _get_windows().get_active_window()
    if not active:
        return False
    return name.lower() in active.title.lower()


def window_exists(name):
    """Check if a window with the given name is open."""
    return _get_windows().is_window_open(name)


def process_running(name):
    """Check if a process with the given name is running."""
    return _get_windows().is_process_running(name)


def file_exists(path):
    """Check if a file exists."""
    return _get_fs().file_exists(path)


def file_has_content(path):
    """Check if a file exists and is non-empty."""
    info = _get_fs().get_file_info(path)
    return info is not None and info.size_bytes > 0


def directory_exists(path):
    """Check if a directory exists."""
    p = _get_fs()._resolve_path(path)
    return p.is_dir()


def tab_count_is(n_str):
    """Check if browser has exactly N tabs."""
    n = int(n_str)
    return _get_browser().get_tab_count() == n


def clipboard_contains(text):
    """Check if clipboard contains specific text."""
    try:
        import pyperclip
        clip = pyperclip.paste()
        return text.lower() in clip.lower()
    except Exception:
        return False


def active_tab_title_contains(text):
    """Check if the active browser tab title contains text."""
    title = _get_browser().get_current_title()
    return text.lower() in title.lower()


def browser_running(*_):
    """Check if any browser is running."""
    return _get_browser().is_browser_running()


# Dispatch table: condition name -> function
_CHECKS = {
    "url_is": url_is,
    "url_contains": url_contains,
    "window_is_focused": window_is_focused,
    "window_exists": window_exists,
    "process_running": process_running,
    "file_exists": file_exists,
    "file_has_content": file_has_content,
    "directory_exists": directory_exists,
    "tab_count_is": tab_count_is,
    "clipboard_contains": clipboard_contains,
    "active_tab_title_contains": active_tab_title_contains,
    "browser_running": browser_running,
}
