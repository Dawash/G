"""
Entry point for the voice AI assistant.
Run: python main.py
"""

import sys

# Fix Windows console encoding for multilingual output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from assistant import run

if __name__ == "__main__":
    run()
