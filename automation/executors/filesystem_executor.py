"""Filesystem executor — typed file operations with state tracking.

Every method captures state_before/state_after via FilesystemObserver
and returns ActionResult with verification.
"""

import logging
import os
import shutil

from automation.executors.base import ActionResult
from automation.observers.filesystem_observer import FilesystemObserver

logger = logging.getLogger(__name__)

_observer = FilesystemObserver()

_BLOCKED_DIRS = frozenset({
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "C:\\ProgramData", "C:\\$Recycle.Bin", "C:\\System Volume Information",
})


def _is_blocked(path_str):
    """Check if path is in a protected system directory."""
    s = str(path_str).rstrip("\\")
    return any(s.startswith(b) for b in _BLOCKED_DIRS)


def _find_locking_process(file_path):
    """Identify which process has a file locked. Returns string or None.

    Uses a 3s time budget and only checks likely document-holding processes.
    """
    try:
        import psutil
        import time as _time
        deadline = _time.monotonic() + 2.0
        file_path = os.path.normpath(os.path.abspath(str(file_path)))
        file_lower = file_path.lower()
        _LIKELY_LOCKERS = {
            "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
            "notepad.exe", "notepad++.exe", "code.exe", "devenv.exe",
            "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe",
            "acrobat.exe", "acrord32.exe", "vlc.exe", "python.exe",
            "node.exe", "java.exe", "sqlservr.exe",
        }
        for proc in psutil.process_iter(["name", "pid"]):
            if _time.monotonic() > deadline:
                break
            pname = (proc.info.get("name") or "").lower()
            if pname not in _LIKELY_LOCKERS:
                continue
            try:
                for f in proc.open_files():
                    if f.path.lower() == file_lower:
                        return f"{proc.info['name']} (PID {proc.info['pid']})"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return None


class FilesystemExecutor:
    """Typed file operations with state tracking."""

    def __init__(self, observer=None):
        self._obs = observer or _observer

    def _resolve(self, path_str):
        return self._obs._resolve_path(path_str)

    def move_file(self, src, dst):
        """Move a file or directory."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)

        if _is_blocked(str(src_path)):
            return ActionResult(ok=False, error=f"Blocked: {src_path} is in a protected directory.")

        before = {"src_exists": src_path.exists(), "dst_exists": dst_path.exists()}

        if not src_path.exists():
            return ActionResult(ok=False, state_before=before, error=f"Source not found: {src_path}")

        try:
            # If dst is a directory, move into it
            if dst_path.is_dir():
                final = dst_path / src_path.name
            else:
                final = dst_path
                final.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(src_path), str(final))

            after = {"src_exists": src_path.exists(), "dst_exists": final.exists()}
            return ActionResult(
                ok=True, strategy_used="shutil",
                state_before=before, state_after=after,
                verified=final.exists() and not src_path.exists(),
                message=f"Moved {src_path.name} to {final.parent}.",
            )
        except PermissionError:
            locker = _find_locking_process(str(src_path))
            msg = f"Permission denied: {src_path.name}"
            if locker:
                msg += f" — locked by {locker}. Close it first."
            return ActionResult(ok=False, state_before=before, error=msg)
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def copy_file(self, src, dst):
        """Copy a file or directory."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)

        before = {"src_exists": src_path.exists()}

        if not src_path.exists():
            return ActionResult(ok=False, state_before=before, error=f"Source not found: {src_path}")

        try:
            if dst_path.is_dir():
                final = dst_path / src_path.name
            else:
                final = dst_path
                final.parent.mkdir(parents=True, exist_ok=True)

            if src_path.is_dir():
                shutil.copytree(str(src_path), str(final))
            else:
                shutil.copy2(str(src_path), str(final))

            after = {"src_exists": src_path.exists(), "dst_exists": final.exists()}
            return ActionResult(
                ok=True, strategy_used="shutil",
                state_before=before, state_after=after,
                verified=final.exists(),
                message=f"Copied {src_path.name} to {final}.",
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def rename_file(self, path, new_name):
        """Rename a file or directory."""
        src = self._resolve(path)

        if not src.exists():
            return ActionResult(ok=False, error=f"Not found: {src}")
        if _is_blocked(str(src)):
            return ActionResult(ok=False, error=f"Blocked: protected directory.")

        before = {"old_name": src.name, "exists": True}
        dst = src.parent / new_name

        try:
            src.rename(dst)
            after = {"new_name": dst.name, "exists": dst.exists()}
            return ActionResult(
                ok=True, strategy_used="os",
                state_before=before, state_after=after,
                verified=dst.exists(),
                message=f"Renamed {src.name} to {new_name}.",
            )
        except PermissionError:
            locker = _find_locking_process(str(src))
            msg = f"Permission denied: {src.name}"
            if locker:
                msg += f" — locked by {locker}. Close it first."
            return ActionResult(ok=False, state_before=before, error=msg)
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def delete_file(self, path):
        """Delete a file or directory.

        Safety: refuses to delete system directories.
        """
        target = self._resolve(path)

        if _is_blocked(str(target)):
            return ActionResult(ok=False, error=f"Blocked: {target} is in a protected directory.")
        if not target.exists():
            return ActionResult(ok=False, error=f"Not found: {target}")

        before = {"path": str(target), "exists": True, "is_dir": target.is_dir()}

        try:
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()

            after = {"exists": target.exists()}
            return ActionResult(
                ok=True, strategy_used="os",
                state_before=before, state_after=after,
                verified=not target.exists(),
                message=f"Deleted {target.name}.",
            )
        except PermissionError:
            locker = _find_locking_process(str(target))
            msg = f"Permission denied: {target.name}"
            if locker:
                msg += f" — locked by {locker}. Close it first."
            return ActionResult(ok=False, state_before=before, error=msg)
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def create_directory(self, path):
        """Create a directory (and parents)."""
        target = self._resolve(path)
        before = {"exists": target.exists()}

        try:
            target.mkdir(parents=True, exist_ok=True)
            after = {"exists": target.exists(), "is_dir": target.is_dir()}
            return ActionResult(
                ok=True, strategy_used="os",
                state_before=before, state_after=after,
                verified=target.is_dir(),
                message=f"Created directory {target}.",
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def zip_files(self, paths, output):
        """Zip files into an archive."""
        import zipfile

        out_path = self._resolve(output)
        before = {"output_exists": out_path.exists()}

        try:
            with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    full = self._resolve(p)
                    if full.exists():
                        if full.is_dir():
                            for f in full.rglob("*"):
                                zf.write(str(f), f.relative_to(full.parent))
                        else:
                            zf.write(str(full), full.name)

            after = {"output_exists": out_path.exists(),
                     "output_size": out_path.stat().st_size if out_path.exists() else 0}
            return ActionResult(
                ok=True, strategy_used="zipfile",
                state_before=before, state_after=after,
                verified=out_path.exists(),
                message=f"Created {out_path.name}.",
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def unzip_file(self, path, destination=None):
        """Extract a zip archive."""
        import zipfile

        src = self._resolve(path)
        if not src.exists():
            return ActionResult(ok=False, error=f"Archive not found: {src}")

        dst = self._resolve(destination) if destination else src.parent / src.stem
        before = {"dst_exists": dst.exists()}

        try:
            with zipfile.ZipFile(str(src), "r") as zf:
                zf.extractall(str(dst))

            after = {"dst_exists": dst.exists()}
            return ActionResult(
                ok=True, strategy_used="zipfile",
                state_before=before, state_after=after,
                verified=dst.exists(),
                message=f"Extracted to {dst}.",
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def list_directory(self, path=None, pattern=None):
        """List directory contents.

        Returns ActionResult with entries in state_after.
        """
        entries = self._obs.list_directory(path, pattern)
        data = [
            {"name": f.name, "is_dir": f.is_dir,
             "size": f.size_human, "modified": f.modified_str}
            for f in entries
        ]
        return ActionResult(
            ok=True, strategy_used="os",
            state_after={"entries": data, "count": len(data)},
            verified=True,
            message=f"{len(data)} items found.",
        )
