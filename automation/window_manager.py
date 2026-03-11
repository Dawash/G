"""
Window management — list, snap, arrange, switch windows.

Provides high-level window operations using pygetwindow + pywinauto.
Replaces coordinate-based window manipulation with structured commands.
"""

import logging
import time

logger = logging.getLogger(__name__)


def list_windows(include_system=False):
    """List all visible windows with metadata.

    Args:
        include_system: Include system windows (taskbar, etc).

    Returns:
        list of dicts: {title, process_name, left, top, width, height, minimized}
    """
    _SYSTEM_TITLES = {
        "", "Program Manager", "Default IME", "MSCTFIME UI",
        "Windows Input Experience", "TextInputHost",
        "Microsoft Text Input Application",
    }

    try:
        import pygetwindow as gw
        all_wins = gw.getAllWindows()
        result = []

        for w in all_wins:
            if not w.title or not w.title.strip():
                continue
            if not include_system and w.title in _SYSTEM_TITLES:
                continue
            if not include_system and w.width < 50 and w.height < 50:
                continue

            info = {
                "title": w.title,
                "left": w.left, "top": w.top,
                "width": w.width, "height": w.height,
                "minimized": w.isMinimized,
            }

            # Get process name
            try:
                from automation.ui_control import _get_desktop
                desktop = _get_desktop()
                uia_wins = desktop.windows(title=w.title, visible_only=False)
                if uia_wins:
                    pid = uia_wins[0].element_info.process_id
                    import psutil
                    proc = psutil.Process(pid)
                    info["process_name"] = proc.name()
            except Exception:
                info["process_name"] = ""

            result.append(info)

        return result
    except Exception as e:
        logger.error(f"list_windows error: {e}")
        return []


def snap_window(name, position):
    """Snap a window to a screen position.

    Args:
        name: Window title or app name.
        position: "left", "right", "maximize", "minimize", "restore",
                  "top-left", "top-right", "bottom-left", "bottom-right".

    Returns:
        str: Result message.
    """
    try:
        import pygetwindow as gw
    except ImportError:
        return "pygetwindow not available."

    # Find window
    windows = gw.getWindowsWithTitle(name)
    if not windows:
        # Fuzzy search
        all_wins = gw.getAllWindows()
        name_lower = name.lower()
        for w in all_wins:
            if w.title and name_lower in w.title.lower():
                windows = [w]
                break

    if not windows:
        return f"Window '{name}' not found."

    win = windows[0]
    title = win.title

    if win.isMinimized:
        win.restore()
        time.sleep(0.3)

    try:
        win.activate()
    except Exception:
        pass

    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    # Taskbar height estimate
    taskbar_h = 48

    try:
        if position == "left":
            win.moveTo(0, 0)
            win.resizeTo(screen_w // 2, screen_h - taskbar_h)
        elif position == "right":
            win.moveTo(screen_w // 2, 0)
            win.resizeTo(screen_w // 2, screen_h - taskbar_h)
        elif position == "top-left":
            win.moveTo(0, 0)
            win.resizeTo(screen_w // 2, (screen_h - taskbar_h) // 2)
        elif position == "top-right":
            win.moveTo(screen_w // 2, 0)
            win.resizeTo(screen_w // 2, (screen_h - taskbar_h) // 2)
        elif position == "bottom-left":
            win.moveTo(0, (screen_h - taskbar_h) // 2)
            win.resizeTo(screen_w // 2, (screen_h - taskbar_h) // 2)
        elif position == "bottom-right":
            win.moveTo(screen_w // 2, (screen_h - taskbar_h) // 2)
            win.resizeTo(screen_w // 2, (screen_h - taskbar_h) // 2)
        elif position == "maximize":
            win.maximize()
        elif position == "minimize":
            win.minimize()
        elif position == "restore":
            win.restore()
        elif position == "center":
            w, h = win.width, win.height
            win.moveTo((screen_w - w) // 2, (screen_h - taskbar_h - h) // 2)
        else:
            return f"Unknown position: {position}. Use: left, right, maximize, minimize, center, top-left, top-right, bottom-left, bottom-right."

        return f"Snapped '{title}' to {position}."
    except Exception as e:
        logger.error(f"snap_window error: {e}")
        return f"Error snapping '{title}': {e}"


def arrange_windows(names, layout="side-by-side"):
    """Arrange multiple windows in a layout.

    Args:
        names: List of window titles/app names.
        layout: "side-by-side" (default), "stacked", "grid".

    Returns:
        str: Result message.
    """
    if not names or len(names) < 2:
        return "Need at least 2 window names to arrange."

    positions = {
        "side-by-side": {
            2: ["left", "right"],
            3: ["left", "top-right", "bottom-right"],
            4: ["top-left", "top-right", "bottom-left", "bottom-right"],
        },
        "stacked": {
            2: ["left", "right"],  # Same as side-by-side for 2
        },
        "grid": {
            4: ["top-left", "top-right", "bottom-left", "bottom-right"],
        },
    }

    count = len(names)
    layout_map = positions.get(layout, positions["side-by-side"])
    pos_list = layout_map.get(count)

    if not pos_list:
        # Default: split evenly left-to-right
        pos_list = ["left", "right"] if count == 2 else ["left"] + ["right"] * (count - 1)

    results = []
    for i, name in enumerate(names[:len(pos_list)]):
        pos = pos_list[i]
        result = snap_window(name, pos)
        results.append(result)
        time.sleep(0.2)

    return " | ".join(results)


def minimize_all():
    """Minimize all windows (show desktop).

    Returns:
        str: Result message.
    """
    try:
        import pyautogui
        pyautogui.hotkey("win", "d")
        return "Minimized all windows (showing desktop)."
    except Exception as e:
        return f"Error minimizing all: {e}"


def close_window(name):
    """Close a window by name.

    Args:
        name: Window title or app name.

    Returns:
        str: Result message.
    """
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(name)
        if not windows:
            all_wins = gw.getAllWindows()
            name_lower = name.lower()
            for w in all_wins:
                if w.title and name_lower in w.title.lower():
                    windows = [w]
                    break

        if not windows:
            return f"Window '{name}' not found."

        title = windows[0].title
        windows[0].close()
        return f"Closed '{title}'."
    except Exception as e:
        return f"Error closing '{name}': {e}"
