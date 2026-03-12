"""
UI Automation control — structured desktop interaction via Microsoft UI Automation.

Provides semantic control over Windows applications without pixel-based
automation. Uses pywinauto's UIA backend to interact with the accessibility
tree directly.

Resolution hierarchy (most reliable first):
  1. UIA InvokePattern / ValuePattern  (direct programmatic control)
  2. UIA coordinates + click           (element found, click center)
  3. Keyboard shortcuts                (app-specific hotkeys)
  4. Vision / screenshot fallback      (last resort — handled elsewhere)

This module handles tiers 1-2. Tier 3 is in computer.py (press_key).
Tier 4 is in vision.py (find_element).
"""

import logging
import time

logger = logging.getLogger(__name__)

# Lazy-loaded pywinauto
_desktop_uia = None


def _get_desktop():
    """Lazy-load pywinauto Desktop with UIA backend."""
    global _desktop_uia
    if _desktop_uia is None:
        try:
            from pywinauto import Desktop
            _desktop_uia = Desktop(backend="uia")
        except ImportError:
            raise RuntimeError("pywinauto is not installed. Install: pip install pywinauto")
    return _desktop_uia


# ===================================================================
# Window finding and focusing
# ===================================================================

def find_window(name):
    """Find a window by title or app name with fuzzy matching.

    Searches visible windows in order:
      1. Exact title match
      2. Title contains name (case-insensitive)
      3. Process name match
      4. Fuzzy title match (SequenceMatcher)

    Args:
        name: Window title, app name, or process name to find.

    Returns:
        pywinauto window wrapper, or None if not found.
    """
    if not name:
        return None

    # Titles to skip (system windows that aren't real user windows)
    _SKIP_TITLES = {"Program Manager", "Default IME", "MSCTFIME UI", ""}

    desktop = _get_desktop()
    name_lower = name.lower().strip()
    all_windows = desktop.windows(visible_only=True)

    if not all_windows:
        # Fallback: try pygetwindow which catches UWP/ApplicationFrameHost windows
        try:
            import pygetwindow as gw
            gw_wins = gw.getWindowsWithTitle(name)
            if gw_wins:
                # Re-find via UIA using the exact title
                uia_wins = desktop.windows(title=gw_wins[0].title, visible_only=True)
                if uia_wins:
                    return uia_wins[0]
        except Exception:
            pass
        return None

    # Filter out system windows for search
    user_windows = []
    for w in all_windows:
        try:
            title = w.element_info.name or ""
            if title not in _SKIP_TITLES:
                user_windows.append(w)
        except Exception:
            continue

    # 1. Exact title match
    for w in user_windows:
        try:
            title = w.element_info.name or ""
            if title.lower() == name_lower:
                return w
        except Exception:
            continue

    # 2. Title contains name
    for w in user_windows:
        try:
            title = (w.element_info.name or "").lower()
            if name_lower in title:
                return w
        except Exception:
            continue

    # 2b. Fallback: try pygetwindow (catches UWP apps, minimized windows)
    try:
        import pygetwindow as gw
        gw_wins = gw.getWindowsWithTitle(name)
        if not gw_wins:
            # Partial match
            for w in gw.getAllWindows():
                if w.title and name_lower in w.title.lower() and w.title not in _SKIP_TITLES:
                    gw_wins = [w]
                    break
        if gw_wins:
            title = gw_wins[0].title
            # Try visible first, then all windows
            uia_wins = desktop.windows(title=title, visible_only=True)
            if not uia_wins:
                uia_wins = desktop.windows(title=title, visible_only=False)
            if uia_wins:
                return uia_wins[0]
            # If UIA can't find it, try title_re (partial match)
            uia_wins = desktop.windows(title_re=f".*{_re_escape(title[:30])}.*",
                                       visible_only=False)
            if uia_wins:
                return uia_wins[0]
    except Exception:
        pass

    # 3. Process name match (skip system shell processes)
    try:
        import psutil
        for w in user_windows:
            try:
                pid = w.element_info.process_id
                proc = psutil.Process(pid)
                proc_name = proc.name().lower().replace(".exe", "")
                if name_lower in proc_name or proc_name in name_lower:
                    return w
            except Exception:
                continue
    except ImportError:
        pass

    # 4. Fuzzy match
    try:
        from difflib import SequenceMatcher
        best_match = None
        best_ratio = 0.0
        for w in all_windows:
            try:
                title = w.element_info.name or ""
                if not title.strip():
                    continue
                ratio = SequenceMatcher(None, name_lower, title.lower()).ratio()
                if ratio > best_ratio and ratio >= 0.5:
                    best_ratio = ratio
                    best_match = w
            except Exception:
                continue
        if best_match:
            return best_match
    except Exception:
        pass

    return None


def focus_window(name):
    """Find and activate a window by name.

    Restores if minimized, brings to foreground.

    Args:
        name: Window title or app name.

    Returns:
        str: Result message.
    """
    win = find_window(name)
    if not win:
        return f"Window '{name}' not found."

    title = win.element_info.name or name
    try:
        # Restore if minimized
        try:
            if win.is_minimized():
                win.restore()
                time.sleep(0.3)
        except Exception:
            pass

        # Activate (bring to front)
        try:
            win.set_focus()
        except Exception:
            # Fallback 1: use pygetwindow
            activated = False
            try:
                import pygetwindow as gw
                gw_wins = gw.getWindowsWithTitle(title)
                if gw_wins:
                    if gw_wins[0].isMinimized:
                        gw_wins[0].restore()
                    gw_wins[0].activate()
                    activated = True
            except Exception:
                pass

            # Fallback 2: Win32 API SetForegroundWindow (most reliable)
            if not activated:
                try:
                    import ctypes
                    import pygetwindow as gw
                    user32 = ctypes.windll.user32
                    gw_wins = gw.getWindowsWithTitle(title)
                    if gw_wins:
                        hwnd = gw_wins[0]._hWnd
                        if user32.IsIconic(hwnd):
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.keybd_event(0x12, 0, 0, 0)  # Alt down
                        user32.keybd_event(0x12, 0, 2, 0)  # Alt up
                        user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

        return f"Focused window: {title}"
    except Exception as e:
        logger.error(f"focus_window error: {e}")
        return f"Error focusing '{name}': {e}"


def get_active_window_info():
    """Get structured info about the currently active window.

    Returns:
        dict with: title, process_name, process_id, rect, class_name.
        Returns None if no active window.
    """
    try:
        import pygetwindow as gw
        active = gw.getActiveWindow()
        if not active or not active.title:
            return None

        info = {
            "title": active.title,
            "rect": {
                "left": active.left, "top": active.top,
                "width": active.width, "height": active.height,
            },
        }

        # Get process info
        try:
            desktop = _get_desktop()
            uia_wins = desktop.windows(title=active.title, visible_only=True)
            if uia_wins:
                w = uia_wins[0]
                info["process_id"] = w.element_info.process_id
                info["class_name"] = w.element_info.class_name or ""
                try:
                    import psutil
                    proc = psutil.Process(w.element_info.process_id)
                    info["process_name"] = proc.name()
                except Exception:
                    pass
        except Exception:
            pass

        return info
    except Exception as e:
        logger.debug(f"get_active_window_info error: {e}")
        return None


# ===================================================================
# Control tree traversal
# ===================================================================

# Control types that are interactable
_CLICKABLE_TYPES = {
    "Button", "Hyperlink", "MenuItem", "TabItem",
    "ListItem", "TreeItem", "CheckBox", "RadioButton",
    "ComboBox", "Slider", "Image", "SplitButton",
    "MenuBar", "ToolBar",
}
_INPUT_TYPES = {"Edit", "Document", "TextBox"}
_ALL_INTERACTIVE = _CLICKABLE_TYPES | _INPUT_TYPES


def list_controls(window=None, role=None, name_filter=None,
                  max_depth=4, max_count=30):
    """List interactive controls in a window via UI Automation tree.

    Args:
        window: Window title to inspect (None = active window).
        role: Filter by control type (e.g. "Button", "Edit").
        name_filter: Filter by name substring.
        max_depth: Max tree depth to traverse (default 4).
        max_count: Max controls to return (default 30).

    Returns:
        list of dicts: {name, type, x, y, width, height, clickable, editable}
    """
    try:
        desktop = _get_desktop()
    except RuntimeError:
        return []

    try:
        # Find target window
        if window:
            wins = desktop.windows(title_re=f".*{_re_escape(window)}.*",
                                   visible_only=True)
            if not wins:
                return []
            win = wins[0]
        else:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if not active or not active.title:
                return []
            wins = desktop.windows(title=active.title, visible_only=True)
            if not wins:
                return []
            win = wins[0]

        target_types = {role} if role else _ALL_INTERACTIVE
        elements = []

        def _traverse(ctrl, depth=0):
            if depth > max_depth or len(elements) >= max_count:
                return
            try:
                ctrl_type = ctrl.element_info.control_type or ""
                ctrl_name = ctrl.element_info.name or ""

                if ctrl_type in target_types and ctrl_name.strip():
                    # Apply name filter
                    if name_filter and name_filter.lower() not in ctrl_name.lower():
                        pass  # skip
                    else:
                        try:
                            rect = ctrl.element_info.rectangle
                            cx = (rect.left + rect.right) // 2
                            cy = (rect.top + rect.bottom) // 2
                            w = rect.right - rect.left
                            h = rect.bottom - rect.top
                            if cx > 0 and cy > 0 and w > 2 and h > 2:
                                elements.append({
                                    "name": ctrl_name.strip()[:120],
                                    "type": ctrl_type,
                                    "x": cx, "y": cy,
                                    "width": w, "height": h,
                                    "clickable": ctrl_type in _CLICKABLE_TYPES,
                                    "editable": ctrl_type in _INPUT_TYPES,
                                })
                        except Exception:
                            pass

                for child in ctrl.children():
                    if len(elements) >= max_count:
                        break
                    _traverse(child, depth + 1)
            except Exception:
                pass

        _traverse(win)
        return elements

    except Exception as e:
        logger.debug(f"list_controls error: {e}")
        return []


def _re_escape(s):
    """Escape special regex characters in a string."""
    import re
    return re.escape(s)


# ===================================================================
# Find and interact with controls
# ===================================================================

def find_control(name=None, role=None, automation_id=None, window=None):
    """Find a specific control in the UI tree.

    Searches by name (fuzzy), role/type, and/or automation ID.

    Args:
        name: Control name or text (fuzzy matched).
        role: Control type (e.g. "Button", "Edit", "Hyperlink").
        automation_id: UIA AutomationId property.
        window: Window title to search in (None = active).

    Returns:
        dict with control info, or None if not found.
    """
    if not name and not role and not automation_id:
        return None

    # If automation_id specified, use pywinauto's direct search
    if automation_id:
        try:
            desktop = _get_desktop()
            if window:
                wins = desktop.windows(title_re=f".*{_re_escape(window)}.*",
                                       visible_only=True)
            else:
                import pygetwindow as gw
                active = gw.getActiveWindow()
                if not active:
                    return None
                wins = desktop.windows(title=active.title, visible_only=True)
            if wins:
                try:
                    ctrl = wins[0].child_window(auto_id=automation_id)
                    if ctrl.exists():
                        rect = ctrl.element_info.rectangle
                        return {
                            "name": ctrl.element_info.name or "",
                            "type": ctrl.element_info.control_type or "",
                            "x": (rect.left + rect.right) // 2,
                            "y": (rect.top + rect.bottom) // 2,
                            "width": rect.right - rect.left,
                            "height": rect.bottom - rect.top,
                            "automation_id": automation_id,
                            "_wrapper": ctrl,
                        }
                except Exception:
                    pass
        except Exception:
            pass

    # Search by name/role using tree traversal
    max_results = 50 if role else 40
    controls = list_controls(window=window, role=role, max_count=max_results,
                             max_depth=5)
    if not controls:
        return None

    if not name:
        return controls[0] if controls else None

    name_lower = name.lower().strip()

    # Exact match
    for c in controls:
        if c["name"].lower().strip() == name_lower:
            return c

    # Partial match
    for c in controls:
        if name_lower in c["name"].lower():
            return c

    # Fuzzy match
    try:
        from difflib import get_close_matches
        all_names = [c["name"] for c in controls if c["name"]]
        matches = get_close_matches(name, all_names, n=1, cutoff=0.45)
        if matches:
            for c in controls:
                if c["name"] == matches[0]:
                    return c
    except Exception:
        pass

    return None


def click_control(name=None, role=None, automation_id=None, window=None):
    """Find a UI control and click it.

    Tries in order:
      1. UIA InvokePattern (programmatic, no coordinates needed)
      2. Click at control center coordinates

    Args:
        name: Control name/text to find.
        role: Control type filter (e.g. "Button").
        automation_id: UIA AutomationId.
        window: Window to search in (None = active).

    Returns:
        str: Result message.
    """
    ctrl_info = find_control(name=name, role=role,
                             automation_id=automation_id, window=window)
    if not ctrl_info:
        # Build useful error message
        available = list_controls(window=window, max_count=15)
        if available:
            names = [c["name"][:40] for c in available[:10] if c["name"]]
            return (f"Control '{name or role or automation_id}' not found. "
                    f"Available: {', '.join(names)}")
        return f"Control '{name or role or automation_id}' not found in window."

    ctrl_name = ctrl_info["name"]

    # Try 1: UIA InvokePattern (most reliable for buttons/links)
    wrapper = ctrl_info.get("_wrapper")
    if wrapper:
        try:
            wrapper.invoke()
            return f"Invoked '{ctrl_name}'"
        except Exception:
            pass

    # Try to get a wrapper for InvokePattern
    if ctrl_info["type"] in ("Button", "Hyperlink", "MenuItem", "SplitButton"):
        try:
            desktop = _get_desktop()
            if window:
                wins = desktop.windows(title_re=f".*{_re_escape(window)}.*",
                                       visible_only=True)
            else:
                import pygetwindow as gw
                active = gw.getActiveWindow()
                wins = desktop.windows(title=active.title,
                                       visible_only=True) if active else []
            if wins:
                try:
                    child = wins[0].child_window(title=ctrl_name,
                                                 control_type=ctrl_info["type"])
                    if child.exists():
                        child.invoke()
                        return f"Invoked '{ctrl_name}'"
                except Exception:
                    pass
        except Exception:
            pass

    # Try 2: Click at center coordinates
    x, y = ctrl_info["x"], ctrl_info["y"]
    if x > 0 and y > 0:
        try:
            import pyautogui
            pyautogui.click(x, y)
            return f"Clicked '{ctrl_name}' at ({x}, {y})"
        except Exception as e:
            return f"Error clicking '{ctrl_name}': {e}"

    return f"Found '{ctrl_name}' but could not click it."


def set_control_text(name=None, text="", role=None, window=None):
    """Set text on an input control.

    Tries in order:
      1. UIA ValuePattern.SetValue() (direct, instant)
      2. Click control + select all + paste (clipboard method)
      3. Click control + select all + type (keystroke method)

    Args:
        name: Control name/label to find.
        text: Text to set.
        role: Control type filter (default: searches Edit/TextBox/Document).
        window: Window to search in.

    Returns:
        str: Result message.
    """
    search_role = role or None
    ctrl_info = find_control(name=name, role=search_role, window=window)

    # If no match by name, try to find any editable control
    if not ctrl_info and name:
        all_editable = list_controls(window=window, max_count=20)
        editable = [c for c in all_editable if c.get("editable")]
        if editable:
            name_lower = name.lower()
            for c in editable:
                if name_lower in c["name"].lower():
                    ctrl_info = c
                    break
            if not ctrl_info:
                ctrl_info = editable[0]  # Use first editable control

    if not ctrl_info:
        return f"No input control '{name or 'any'}' found."

    ctrl_name = ctrl_info["name"]
    x, y = ctrl_info["x"], ctrl_info["y"]

    # Try 1: UIA ValuePattern
    try:
        desktop = _get_desktop()
        import pygetwindow as gw
        if window:
            wins = desktop.windows(title_re=f".*{_re_escape(window)}.*",
                                   visible_only=True)
        else:
            active = gw.getActiveWindow()
            wins = desktop.windows(title=active.title,
                                   visible_only=True) if active else []
        if wins:
            try:
                child = wins[0].child_window(title=ctrl_name,
                                             control_type=ctrl_info["type"])
                if child.exists():
                    from pywinauto.controls.uiawrapper import UIAWrapper
                    iface = child.iface_value
                    if iface:
                        iface.SetValue(text)
                        return f"Set '{ctrl_name}' to '{text[:50]}'"
            except Exception:
                pass
    except Exception:
        pass

    # Try 2: Click + paste
    if x > 0 and y > 0:
        try:
            import pyautogui
            pyautogui.click(x, y)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)

            try:
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
            except ImportError:
                pyautogui.typewrite(text, interval=0.02)

            return f"Typed into '{ctrl_name}': '{text[:50]}'"
        except Exception as e:
            return f"Error setting text on '{ctrl_name}': {e}"

    return f"Found '{ctrl_name}' but could not set text."


def invoke_control(name=None, role=None, automation_id=None, window=None):
    """Invoke a control's default action via UIA InvokePattern.

    Works for: buttons, hyperlinks, menu items.
    More reliable than clicking — works even if window is partially obscured.

    Returns:
        str: Result message.
    """
    return click_control(name=name, role=role,
                         automation_id=automation_id, window=window)


def get_focused_element():
    """Get info about the currently focused UI element.

    Returns:
        dict with name, type, value, rect, or None.
    """
    try:
        from pywinauto.uia_defines import IUIA
        from comtypes import COMError

        iuia = IUIA()
        focused = iuia.iuia.GetFocusedElement()
        if not focused:
            return None

        from pywinauto.uia_element_info import UIAElementInfo
        info = UIAElementInfo(focused)

        result = {
            "name": info.name or "",
            "type": info.control_type or "",
            "class_name": info.class_name or "",
        }

        try:
            rect = info.rectangle
            result["x"] = (rect.left + rect.right) // 2
            result["y"] = (rect.top + rect.bottom) // 2
            result["width"] = rect.right - rect.left
            result["height"] = rect.bottom - rect.top
        except Exception:
            pass

        return result
    except Exception as e:
        logger.debug(f"get_focused_element error: {e}")
        return None


# ===================================================================
# Window inspection
# ===================================================================

def inspect_window(name=None, max_controls=20):
    """Get structured overview of a window's content.

    Returns window metadata + top interactive controls. Replaces
    screenshot-based observation for structural state queries.

    Args:
        name: Window title (None = active window).
        max_controls: Max controls to include.

    Returns:
        str: Formatted window inspection report.
    """
    # Get window info
    if name:
        win = find_window(name)
        if not win:
            return f"Window '{name}' not found."
        title = win.element_info.name or name
    else:
        info = get_active_window_info()
        if not info:
            return "No active window."
        title = info["title"]
        name = title

    # Get process info
    proc_name = ""
    try:
        if name:
            win_ref = find_window(name)
            if win_ref:
                pid = win_ref.element_info.process_id
                import psutil
                proc = psutil.Process(pid)
                proc_name = proc.name()
    except Exception:
        pass

    # Get controls
    controls = list_controls(window=name, max_count=max_controls)

    # Format output
    lines = [f"Window: {title}"]
    if proc_name:
        lines.append(f"Process: {proc_name}")

    if controls:
        lines.append(f"Controls ({len(controls)}):")
        for c in controls:
            icon = ">" if c.get("editable") else "*" if c.get("clickable") else "-"
            lines.append(f"  {icon} [{c['type']}] {c['name'][:60]} "
                        f"({c['x']},{c['y']})")
    else:
        lines.append("No interactive controls found.")

    # Focused element
    focused = get_focused_element()
    if focused and focused.get("name"):
        lines.append(f"Focused: [{focused['type']}] {focused['name'][:60]}")

    return "\n".join(lines)
