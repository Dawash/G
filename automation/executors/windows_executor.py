"""Windows & process executor — typed operations with state tracking.

Every method captures state_before/state_after via WindowsObserver
and returns ActionResult with verification.
"""

import logging
import time

from automation.executors.base import ActionResult
from automation.observers.windows_observer import WindowsObserver

logger = logging.getLogger(__name__)

# Shared observer instance
_observer = WindowsObserver()


class WindowsExecutor:
    """Typed window/process operations with state tracking."""

    # Settle delay after focus/activate — gives heavy apps time to foreground.
    # Increase if type_text keystrokes go to the wrong window.
    FOCUS_SETTLE_MS = 400

    def __init__(self, observer=None):
        self._obs = observer or _observer

    def _snapshot(self):
        """Quick state snapshot for before/after comparison."""
        active = self._obs.get_active_window()
        return {
            "active_window": active.title if active else "",
            "active_process": active.process_name if active else "",
        }

    def focus_window(self, name):
        """Bring a window to the foreground.

        Tier 1: pygetwindow.activate() with retry
        Tier 2: UIA set_focus()
        """
        before = self._snapshot()
        strategy = ""
        settle = self.FOCUS_SETTLE_MS / 1000.0

        # Tier 1: pygetwindow
        try:
            import pygetwindow as gw
            windows = gw.getWindowsWithTitle(name)
            if not windows:
                # Fuzzy search
                for w in gw.getAllWindows():
                    if w.title and name.lower() in w.title.lower():
                        windows = [w]
                        break
            if windows:
                win = windows[0]
                if win.isMinimized:
                    win.restore()
                    time.sleep(0.3)
                win.activate()
                strategy = "pygetwindow"
                time.sleep(settle)

                after = self._snapshot()
                focused = name.lower() in after.get("active_window", "").lower()

                # Retry once if verification failed (heavy apps need extra time)
                if not focused:
                    win.activate()
                    time.sleep(settle)
                    after = self._snapshot()
                    focused = name.lower() in after.get("active_window", "").lower()

                return ActionResult(
                    ok=focused, strategy_used=strategy,
                    state_before=before, state_after=after,
                    verified=focused,
                    message=f"Switched to {win.title}." if focused
                            else f"Activated {win.title} but verification uncertain.",
                )
        except Exception as e:
            logger.debug(f"pygetwindow focus error: {e}")

        # Tier 2: UIA
        try:
            from automation.ui_control import find_window
            uia_win = find_window(name)
            if uia_win:
                uia_win.set_focus()
                strategy = "uia"
                time.sleep(0.3)

                after = self._snapshot()
                focused = name.lower() in after.get("active_window", "").lower()
                return ActionResult(
                    ok=focused, strategy_used=strategy,
                    state_before=before, state_after=after,
                    verified=focused,
                    message=f"Focused {name}.",
                )
        except Exception as e:
            logger.debug(f"UIA focus error: {e}")

        return ActionResult(
            ok=False, strategy_used="none",
            state_before=before, state_after=before,
            error=f"Window '{name}' not found.",
        )

    def close_window(self, name):
        """Close a window by name.

        Tier 1: pygetwindow.close()
        Tier 2: taskkill
        """
        before = self._snapshot()
        was_open = self._obs.is_window_open(name)
        if not was_open:
            return ActionResult(
                ok=False, state_before=before, state_after=before,
                error=f"Window '{name}' not found.",
            )

        # Tier 1: pygetwindow
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if w.title and name.lower() in w.title.lower():
                    title = w.title
                    w.close()
                    time.sleep(0.5)

                    still_open = self._obs.is_window_open(name)
                    after = self._snapshot()
                    return ActionResult(
                        ok=not still_open, strategy_used="pygetwindow",
                        state_before=before, state_after=after,
                        verified=not still_open,
                        message=f"Closed {title}.",
                    )
        except Exception as e:
            logger.debug(f"pygetwindow close error: {e}")

        # Tier 2: taskkill
        try:
            import subprocess
            result = subprocess.run(
                ["taskkill", "/IM", f"{name}.exe", "/F"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
            time.sleep(0.5)
            still_open = self._obs.is_window_open(name)
            after = self._snapshot()
            return ActionResult(
                ok=not still_open, strategy_used="taskkill",
                state_before=before, state_after=after,
                verified=not still_open,
                message=f"Killed {name}.",
            )
        except Exception as e:
            logger.debug(f"taskkill error: {e}")

        return ActionResult(
            ok=False, strategy_used="none",
            state_before=before, state_after=before,
            error=f"Failed to close '{name}'.",
        )

    def snap_window(self, name, position):
        """Snap a window to a screen position (left, right, top, bottom, maximize).

        Detects which monitor the window is currently on for multi-monitor setups.
        """
        before = self._snapshot()
        geom_before = self._obs.get_window_geometry(name)

        try:
            import pygetwindow as gw
            windows = gw.getWindowsWithTitle(name)
            if not windows:
                for w in gw.getAllWindows():
                    if w.title and name.lower() in w.title.lower():
                        windows = [w]
                        break
            if not windows:
                return ActionResult(
                    ok=False, state_before=before,
                    error=f"Window '{name}' not found.",
                )

            win = windows[0]
            if win.isMinimized:
                win.restore()
                time.sleep(0.3)

            # Detect monitor the window is on
            screen_x, screen_y, screen_w, screen_h = self._get_monitor_for_window(win)

            pos = position.lower().strip()
            if pos in ("left", "l"):
                win.moveTo(screen_x, screen_y)
                win.resizeTo(screen_w // 2, screen_h)
            elif pos in ("right", "r"):
                win.moveTo(screen_x + screen_w // 2, screen_y)
                win.resizeTo(screen_w // 2, screen_h)
            elif pos in ("top", "t", "top-half"):
                win.moveTo(screen_x, screen_y)
                win.resizeTo(screen_w, screen_h // 2)
            elif pos in ("bottom", "b", "bottom-half"):
                win.moveTo(screen_x, screen_y + screen_h // 2)
                win.resizeTo(screen_w, screen_h // 2)
            elif pos in ("maximize", "max", "full", "fullscreen"):
                win.maximize()
            elif pos in ("center", "centre"):
                w, h = win.width, win.height
                win.moveTo(screen_x + (screen_w - w) // 2,
                           screen_y + (screen_h - h) // 2)
            else:
                return ActionResult(
                    ok=False, state_before=before,
                    error=f"Unknown snap position: {position}. Use left/right/top/bottom/maximize/center.",
                )

            time.sleep(0.3)
            geom_after = self._obs.get_window_geometry(name)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="pygetwindow",
                state_before={**before, "geometry": geom_before},
                state_after={**after, "geometry": geom_after},
                verified=geom_before != geom_after,
                message=f"Snapped {win.title} to {pos}.",
            )
        except Exception as e:
            return ActionResult(
                ok=False, state_before=before, state_after=before,
                error=str(e),
            )

    @staticmethod
    def _get_monitor_for_window(win):
        """Determine which monitor a window is on. Returns (x, y, w, h).

        Uses screeninfo if available, falls back to primary screen via ctypes.
        """
        win_cx = win.left + win.width // 2
        win_cy = win.top + win.height // 2

        try:
            from screeninfo import get_monitors
            for m in get_monitors():
                if (m.x <= win_cx < m.x + m.width
                        and m.y <= win_cy < m.y + m.height):
                    return m.x, m.y, m.width, m.height
            # Window not on any monitor — use primary
            for m in get_monitors():
                if m.is_primary:
                    return m.x, m.y, m.width, m.height
        except ImportError:
            pass

        # Fallback: primary screen via ctypes
        try:
            import ctypes
            user32 = ctypes.windll.user32
            return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            return 0, 0, 1920, 1080  # Assume Full HD

    def open_app(self, name, action_registry=None):
        """Launch an application.

        Uses app_finder for lookup, action_registry for execution.
        Verifies by checking if window/process appeared.
        """
        before = self._snapshot()
        was_running = self._obs.is_process_running(name)

        # Try action_registry (standard path)
        if action_registry and "open_app" in action_registry:
            try:
                result = action_registry["open_app"](name)
                time.sleep(1.0)  # Apps take time to show window

                now_running = self._obs.is_window_open(name)
                after = self._snapshot()
                return ActionResult(
                    ok=now_running or was_running,
                    strategy_used="action_registry",
                    state_before=before, state_after=after,
                    verified=now_running,
                    message=f"Opening {name}." if now_running
                            else str(result) if result else f"Launched {name}.",
                )
            except Exception as e:
                logger.debug(f"action_registry open error: {e}")

        # Direct app_finder
        try:
            from app_finder import find_best_match
            import subprocess
            match = find_best_match(name)
            if match:
                path = match.get("path", "")
                if path:
                    subprocess.Popen(path, shell=True)
                    time.sleep(1.5)
                    now_running = self._obs.is_window_open(name)
                    after = self._snapshot()
                    return ActionResult(
                        ok=True, strategy_used="app_finder",
                        state_before=before, state_after=after,
                        verified=now_running,
                        message=f"Opening {name}.",
                    )
        except Exception as e:
            logger.debug(f"app_finder error: {e}")

        return ActionResult(
            ok=False, state_before=before, state_after=before,
            error=f"Could not find or launch '{name}'.",
        )

    def minimize_window(self, name):
        """Minimize a window."""
        before = self._snapshot()
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if w.title and name.lower() in w.title.lower():
                    w.minimize()
                    time.sleep(0.3)
                    after = self._snapshot()
                    return ActionResult(
                        ok=True, strategy_used="pygetwindow",
                        state_before=before, state_after=after,
                        verified=True,
                        message=f"Minimized {w.title}.",
                    )
        except Exception as e:
            logger.debug(f"minimize error: {e}")

        return ActionResult(
            ok=False, state_before=before, state_after=before,
            error=f"Window '{name}' not found.",
        )

    def minimize_all(self):
        """Minimize all windows (show desktop)."""
        before = self._snapshot()
        try:
            import pyautogui
            pyautogui.hotkey("win", "d")
            time.sleep(0.5)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="keyboard",
                state_before=before, state_after=after,
                verified=True,
                message="Minimized all windows.",
            )
        except Exception as e:
            return ActionResult(
                ok=False, state_before=before, state_after=before,
                error=str(e),
            )
