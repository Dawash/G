"""
Emergency stop and global control flags.

Replaces: assistant.py trigger_emergency_stop(), clear_emergency_stop(),
          is_emergency_stopped(), _start_hotkey_listener()

Centralizes all emergency stop logic so brain.py, desktop_agent.py, and
assistant.py can import from one place without circular dependencies.

The old pattern was:
    brain.py -> from assistant import is_emergency_stopped  (circular!)
    assistant.py -> from desktop_agent import DesktopAgent   (circular!)

New pattern:
    brain.py -> from core.control_flags import is_emergency_stopped
    assistant.py -> from core.control_flags import emergency_stop_service
    desktop_agent.py -> from core.control_flags import is_emergency_stopped
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class EmergencyStopService:
    """Thread-safe emergency stop with callback registration.

    Any module can:
      - Trigger a stop:  emergency_stop_service.trigger()
      - Check the flag:  emergency_stop_service.is_stopped()
      - Clear after ack: emergency_stop_service.clear()
      - Register cleanup: emergency_stop_service.on_stop(my_cleanup_fn)

    Callbacks run synchronously in the triggering thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False
        self._callbacks: list[Callable[[], None]] = []

    def trigger(self) -> None:
        """Halt all automation immediately."""
        with self._lock:
            if self._stopped:
                return  # Already stopped, avoid re-triggering callbacks
            self._stopped = True
            callbacks = list(self._callbacks)

        logger.warning("EMERGENCY STOP triggered")
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Emergency stop callback failed")

    def clear(self) -> None:
        """Reset the flag after stop is acknowledged."""
        with self._lock:
            self._stopped = False

    def is_stopped(self) -> bool:
        """Check if emergency stop is active."""
        with self._lock:
            return self._stopped

    def on_stop(self, callback: Callable[[], None]) -> None:
        """Register a callback to run when emergency stop fires.

        Callbacks run synchronously. Keep them fast and non-blocking.
        Use for: cancelling agents, stopping TTS, aborting subprocesses.
        """
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def off_stop(self, callback: Callable[[], None]) -> None:
        """Unregister a stop callback."""
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton — importable from anywhere without circular deps
# ---------------------------------------------------------------------------

emergency_stop_service = EmergencyStopService()

# Convenience aliases for the most common operations
trigger_emergency_stop = emergency_stop_service.trigger
clear_emergency_stop = emergency_stop_service.clear
is_emergency_stopped = emergency_stop_service.is_stopped


def start_hotkey_listener() -> None:
    """Start background thread: Ctrl+Shift+Escape triggers emergency stop.

    Uses Win32 RegisterHotKey API. Safe to call on non-Windows (returns silently).
    """
    import sys
    if sys.platform != "win32":
        return

    def _listener() -> None:
        try:
            import ctypes
            import ctypes.wintypes

            user32 = ctypes.windll.user32
            MOD_CTRL = 0x0002
            MOD_SHIFT = 0x0004
            VK_ESCAPE = 0x1B
            HOTKEY_ID = 9999
            WM_HOTKEY = 0x0312

            if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CTRL | MOD_SHIFT, VK_ESCAPE):
                logger.warning("Could not register Ctrl+Shift+Escape hotkey (may already be in use)")
                return

            logger.info("Emergency stop hotkey registered: Ctrl+Shift+Escape")
            msg = ctypes.wintypes.MSG()
            while True:
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        trigger_emergency_stop()
        except Exception as e:
            logger.debug(f"Hotkey listener failed: {e}")

    t = threading.Thread(target=_listener, daemon=True)
    t.start()
