"""Windows & process state observer — structured state, no side effects.

Reads window list, active window, running processes, and geometry
via Win32/pygetwindow/psutil. Sub-second execution.
"""

import logging
import time
from dataclasses import dataclass, field

from automation.observers.base import ObservationResult

logger = logging.getLogger(__name__)

_SYSTEM_TITLES = frozenset({
    "", "Program Manager", "Default IME", "MSCTFIME UI",
    "Windows Input Experience", "TextInputHost",
    "Microsoft Text Input Application",
})


@dataclass
class WindowInfo:
    """Structured window state."""
    title: str
    process_name: str = ""
    pid: int = 0
    is_active: bool = False
    is_minimized: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def matches(self, name):
        """Fuzzy match against a window/app name."""
        lower = name.lower()
        return (lower in self.title.lower()
                or lower in self.process_name.lower().replace(".exe", ""))


@dataclass
class ProcessInfo:
    """Structured process state."""
    name: str
    pid: int
    status: str = "running"
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


class WindowsObserver:
    """Reads windows/process state from the OS. No side effects."""

    def __init__(self):
        self._last_observation = None
        # TTL cache for get_all_windows (expensive UIA calls)
        self._windows_cache = []
        self._windows_cache_time = 0.0
        self._windows_cache_ttl = 2.0  # seconds

    def get_all_windows(self, include_system=False):
        """List all visible windows with metadata.

        Uses a TTL cache to avoid repeated expensive UIA process lookups.

        Returns:
            list[WindowInfo]
        """
        now = time.time()
        if (self._windows_cache
                and not include_system
                and (now - self._windows_cache_time) < self._windows_cache_ttl):
            return self._windows_cache

        try:
            import pygetwindow as gw
        except ImportError:
            return []

        windows = []
        try:
            for w in gw.getAllWindows():
                if not w.title or not w.title.strip():
                    continue
                if not include_system and w.title in _SYSTEM_TITLES:
                    continue
                if not include_system and w.width < 50 and w.height < 50:
                    continue

                info = WindowInfo(
                    title=w.title,
                    is_minimized=w.isMinimized,
                    x=w.left, y=w.top,
                    width=w.width, height=w.height,
                )

                # Get process name via UIA
                try:
                    from automation.ui_control import _get_desktop
                    desktop = _get_desktop()
                    uia_wins = desktop.windows(title=w.title, visible_only=False)
                    if uia_wins:
                        pid = uia_wins[0].element_info.process_id
                        info.pid = pid
                        import psutil
                        proc = psutil.Process(pid)
                        info.process_name = proc.name()
                except Exception:
                    pass

                windows.append(info)
        except Exception as e:
            logger.debug(f"get_all_windows error: {e}")

        # Update cache (only for default non-system calls)
        if not include_system:
            self._windows_cache = windows
            self._windows_cache_time = now

        return windows

    def invalidate_cache(self):
        """Force refresh on next get_all_windows() call."""
        self._windows_cache_time = 0.0

    def get_active_window(self):
        """Get the currently focused window.

        Returns:
            WindowInfo or None
        """
        try:
            from automation.ui_control import get_active_window_info
            info = get_active_window_info()
            if not info:
                return None
            return WindowInfo(
                title=info.get("title", ""),
                process_name=info.get("process_name", ""),
                pid=info.get("pid", 0),
                is_active=True,
            )
        except Exception:
            pass

        # Fallback: pygetwindow
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if active and active.title:
                return WindowInfo(title=active.title, is_active=True)
        except Exception:
            pass

        return None

    def is_window_open(self, name):
        """Check if a window with the given name is open.

        Fast path: uses pygetwindow directly without UIA process lookup.
        """
        try:
            import pygetwindow as gw
            lower = name.lower()
            for w in gw.getAllWindows():
                if w.title and lower in w.title.lower():
                    return True
        except Exception:
            pass
        return False

    def find_window(self, name):
        """Find a specific window by name.

        Returns:
            WindowInfo or None
        """
        windows = self.get_all_windows()
        lower = name.lower()
        # Exact title match first
        for w in windows:
            if w.title.lower() == lower:
                return w
        # Partial match
        for w in windows:
            if w.matches(name):
                return w
        return None

    def is_process_running(self, name):
        """Check if a process with the given name is running."""
        try:
            import psutil
            lower = name.lower()
            if not lower.endswith(".exe"):
                lower += ".exe"
            for proc in psutil.process_iter(["name"]):
                if proc.info["name"] and proc.info["name"].lower() == lower:
                    return True
        except Exception:
            pass
        return False

    def get_running_processes(self, user_only=True):
        """Get list of running processes.

        Args:
            user_only: Filter to user processes (exclude system/service processes).

        Returns:
            list[ProcessInfo]
        """
        try:
            import psutil
        except ImportError:
            return []

        processes = []
        _SYSTEM_PROCS = frozenset({
            "system", "svchost.exe", "csrss.exe", "lsass.exe",
            "services.exe", "smss.exe", "wininit.exe", "winlogon.exe",
            "dwm.exe", "conhost.exe", "fontdrvhost.exe", "sihost.exe",
            "taskhostw.exe", "runtimebroker.exe", "searchhost.exe",
        })

        try:
            for proc in psutil.process_iter(["name", "pid", "status",
                                              "cpu_percent", "memory_info"]):
                try:
                    pname = proc.info["name"] or ""
                    if user_only and pname.lower() in _SYSTEM_PROCS:
                        continue
                    mem = proc.info.get("memory_info")
                    processes.append(ProcessInfo(
                        name=pname,
                        pid=proc.info["pid"],
                        status=proc.info.get("status", ""),
                        cpu_percent=proc.info.get("cpu_percent", 0) or 0,
                        memory_mb=round(mem.rss / 1048576, 1) if mem else 0,
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            logger.debug(f"get_running_processes error: {e}")

        return processes

    def get_window_geometry(self, name):
        """Get window position and size.

        Returns:
            dict with x, y, width, height — or None.
        """
        win = self.find_window(name)
        if win:
            return {"x": win.x, "y": win.y,
                    "width": win.width, "height": win.height}
        return None

    def observe(self):
        """Full snapshot of windows/process state.

        Returns:
            ObservationResult
        """
        active = self.get_active_window()
        windows = self.get_all_windows()

        # Mark which one is active
        if active:
            for w in windows:
                if w.title == active.title:
                    w.is_active = True

        data = {
            "active_window": {
                "title": active.title if active else "",
                "process": active.process_name if active else "",
            },
            "window_count": len(windows),
            "windows": [
                {"title": w.title, "process": w.process_name,
                 "minimized": w.is_minimized, "active": w.is_active}
                for w in windows[:20]  # Cap at 20 for sanity
            ],
        }

        self._last_observation = ObservationResult(
            domain="windows",
            data=data,
            source="win32",
            stale_after=3.0,  # Window state changes fast
        )
        return self._last_observation
