"""
Windows platform layer — apps, UI automation, media, files, terminal, settings.

Replaces: actions.py, app_finder.py, computer.py (platform-specific parts),
           brain_defs.py (Windows-specific tool handlers)

This package isolates all Windows-specific code:
  - apps.py: App discovery, launch, close (Registry + Start Menu + fuzzy match)
  - ui_automation.py: Mouse, keyboard, accessibility tree, window management
  - media.py: Spotify/media key control, volume
  - files.py: File management (move, copy, rename, delete, zip, find)
  - terminal.py: PowerShell/CMD execution with safety blocklist
  - settings.py: System settings toggle (dark mode, bluetooth, wifi, etc.)
"""
