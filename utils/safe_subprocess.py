"""Safe subprocess wrapper — always uses UTF-8 encoding on Windows.

Prevents the Windows cp1252 UnicodeEncodeError that occurs when
PowerShell or other tools output Unicode characters.

Usage:
    from utils.safe_subprocess import safe_run
    result = safe_run(["powershell", "-Command", "Get-Process"])
"""

import subprocess


def safe_run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run() wrapper that forces UTF-8 encoding.

    On Windows, subprocess.run(text=True) defaults to cp1252,
    which crashes on Unicode. This forces UTF-8 + replace.
    """
    if kwargs.get("text", False) or kwargs.get("universal_newlines", False):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    kwargs.setdefault("capture_output", True)
    return subprocess.run(cmd, **kwargs)


def safe_check_output(cmd, **kwargs) -> str:
    """subprocess.check_output() wrapper with UTF-8 encoding."""
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("text", True)
    return subprocess.check_output(cmd, **kwargs)
