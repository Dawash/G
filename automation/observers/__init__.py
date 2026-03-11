"""State observers — read structured state, no side effects."""

from automation.observers.base import ObservationResult
from automation.observers.windows_observer import WindowsObserver, WindowInfo, ProcessInfo
from automation.observers.filesystem_observer import FilesystemObserver
from automation.observers.browser_observer import BrowserObserver, TabInfo, PageSnapshot
from automation.observers.system_observer import SystemObserver, SystemInfo

__all__ = [
    "ObservationResult",
    "WindowsObserver", "WindowInfo", "ProcessInfo",
    "FilesystemObserver",
    "BrowserObserver", "TabInfo", "PageSnapshot",
    "SystemObserver", "SystemInfo",
]
