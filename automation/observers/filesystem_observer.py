"""Filesystem state observer — structured state, no side effects.

Reads directory listings, file metadata, disk usage via os/pathlib.
Sub-second execution for typical directories.
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from automation.observers.base import ObservationResult

logger = logging.getLogger(__name__)

_HOME = Path.home()
_BLOCKED_DIRS = frozenset({
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "C:\\ProgramData", "C:\\$Recycle.Bin", "C:\\System Volume Information",
})


@dataclass
class FileInfo:
    """Structured file metadata."""
    name: str
    path: str
    is_dir: bool = False
    size_bytes: int = 0
    modified: float = 0.0
    extension: str = ""

    @property
    def size_human(self):
        """Human-readable size."""
        b = self.size_bytes
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"

    @property
    def modified_str(self):
        if self.modified:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.modified))
        return ""


@dataclass
class DiskInfo:
    """Disk usage info."""
    drive: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


class FilesystemObserver:
    """Reads file system state. No side effects."""

    def __init__(self, home=None):
        self._home = Path(home) if home else _HOME

    def _resolve_path(self, path_str):
        """Resolve relative paths against user home."""
        p = Path(path_str)
        if not p.is_absolute():
            p = self._home / p
        return p

    def _is_blocked(self, path):
        """Check if path is in a protected system directory."""
        path_str = str(path).rstrip("\\")
        for blocked in _BLOCKED_DIRS:
            if path_str.startswith(blocked):
                return True
        return False

    def list_directory(self, path=None, pattern=None, max_items=100):
        """List directory contents with metadata.

        Args:
            path: Directory path (relative to home, or absolute).
            pattern: Glob pattern filter (e.g. "*.pdf").
            max_items: Maximum entries to return.

        Returns:
            list[FileInfo]
        """
        target = self._resolve_path(path) if path else self._home / "Desktop"

        if not target.exists():
            return []
        if not target.is_dir():
            # Single file
            return [self._file_info(target)]

        try:
            entries = []
            if pattern:
                items = list(target.glob(pattern))[:max_items]
            else:
                items = list(target.iterdir())[:max_items]

            for item in items:
                entries.append(self._file_info(item))
            return sorted(entries, key=lambda f: (not f.is_dir, f.name.lower()))
        except PermissionError:
            return []

    def file_exists(self, path):
        """Check if a file/directory exists."""
        return self._resolve_path(path).exists()

    def get_file_info(self, path):
        """Get metadata for a single file.

        Returns:
            FileInfo or None
        """
        p = self._resolve_path(path)
        if not p.exists():
            return None
        return self._file_info(p)

    def find_files(self, pattern, base_path=None, recursive=True, max_results=50):
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g. "*.pdf", "report*").
            base_path: Starting directory (default: home).
            recursive: Search subdirectories.
            max_results: Cap results.

        Returns:
            list[FileInfo]
        """
        base = self._resolve_path(base_path) if base_path else self._home
        try:
            if recursive:
                matches = list(base.rglob(pattern))[:max_results]
            else:
                matches = list(base.glob(pattern))[:max_results]
            return [self._file_info(m) for m in matches]
        except (PermissionError, OSError):
            return []

    def get_disk_usage(self, drive="C:"):
        """Get disk usage for a drive.

        Returns:
            DiskInfo
        """
        import shutil
        try:
            usage = shutil.disk_usage(drive + "\\")
            return DiskInfo(
                drive=drive,
                total_gb=round(usage.total / (1024**3), 1),
                used_gb=round(usage.used / (1024**3), 1),
                free_gb=round(usage.free / (1024**3), 1),
                percent_used=round(100 * usage.used / usage.total, 1),
            )
        except Exception as e:
            logger.debug(f"disk_usage error: {e}")
            return DiskInfo(drive=drive, total_gb=0, used_gb=0,
                           free_gb=0, percent_used=0)

    def get_folder_size(self, path):
        """Calculate total size of a folder.

        Returns:
            int: Total bytes.
        """
        target = self._resolve_path(path)
        total = 0
        try:
            for dirpath, _, filenames in os.walk(target):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
        return total

    def observe(self, path=None):
        """Snapshot of filesystem state at a path.

        Returns:
            ObservationResult
        """
        target = self._resolve_path(path) if path else self._home / "Desktop"
        entries = self.list_directory(str(target), max_items=30)
        disk = self.get_disk_usage()

        data = {
            "path": str(target),
            "entry_count": len(entries),
            "entries": [
                {"name": f.name, "is_dir": f.is_dir,
                 "size": f.size_human, "modified": f.modified_str}
                for f in entries[:30]
            ],
            "disk": {
                "drive": disk.drive,
                "total_gb": disk.total_gb,
                "free_gb": disk.free_gb,
                "percent_used": disk.percent_used,
            },
        }

        return ObservationResult(
            domain="filesystem",
            data=data,
            source="os",
            stale_after=10.0,  # FS state changes less frequently
        )

    def _file_info(self, path):
        """Build FileInfo from a Path object."""
        try:
            stat = path.stat()
            return FileInfo(
                name=path.name,
                path=str(path),
                is_dir=path.is_dir(),
                size_bytes=stat.st_size if not path.is_dir() else 0,
                modified=stat.st_mtime,
                extension=path.suffix.lower(),
            )
        except OSError:
            return FileInfo(name=path.name, path=str(path))
