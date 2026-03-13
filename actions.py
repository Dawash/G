"""
Action handlers — system commands, app control, web actions.

All responses are written in natural conversational language,
as if a person is speaking to you.
"""

import os
import logging
import webbrowser

import pygetwindow as gw
from app_finder import launch_app


# --- Web actions ---

def google_search(query):
    """Open a Google search in the default browser."""
    from urllib.parse import quote_plus
    webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
    return f"I've opened a search for '{query}' in your browser."


# --- App launching (delegates to smart app_finder) ---

def open_application(app_name):
    """Find and launch any application by name using intelligent discovery."""
    return launch_app(app_name)


# --- Window management ---

def minimize_window(title):
    """Minimize windows matching the given title."""
    try:
        windows = gw.getWindowsWithTitle(title)
        if windows:
            for w in windows:
                w.minimize()
            return f"Done, I've minimized {title} for you."
        return f"I couldn't find a window called {title}. Is it open?"
    except Exception as e:
        logging.error(f"Error minimizing window: {e}")
        return f"I tried to minimize {title} but ran into an issue."


def close_window(title):
    """Close windows matching the given title, with event-driven verification.

    Falls back to taskkill for UWP apps (Calculator, etc.) that don't respond to w.close().
    """
    try:
        # File Explorer special case: taskkill explorer.exe kills the shell,
        # so we close Explorer windows via COM (Shell.Application)
        if title.lower() in ("explorer", "file explorer"):
            try:
                import subprocess
                # Use PowerShell to close all Explorer windows via COM
                ps_cmd = (
                    "(New-Object -ComObject Shell.Application).Windows() | "
                    "ForEach-Object { $_.Quit() }"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, timeout=5
                )
                import time
                time.sleep(0.5)
                remaining = gw.getWindowsWithTitle("Explorer")
                if not remaining:
                    return f"Done, I've closed File Explorer."
            except Exception:
                pass

        windows = gw.getWindowsWithTitle(title)
        if windows:
            for w in windows:
                try:
                    w.close()
                except Exception:
                    pass
            # Verify window actually closed
            try:
                from automation.event_waiter import wait_for_window_gone
                result = wait_for_window_gone(title, max_wait=3, interval=0.2)
                if result["gone"]:
                    return f"Done, I've closed {title}."
            except ImportError:
                import time
                time.sleep(1)
                remaining = gw.getWindowsWithTitle(title)
                if not remaining:
                    return f"Done, I've closed {title}."

            # w.close() didn't work — try taskkill (needed for UWP apps like Calculator)
            _UWP_PROCESS_MAP = {
                "calculator": "CalculatorApp.exe",
                "photos": "Microsoft.Photos.exe",
                "movies": "Video.UI.exe",
                "camera": "WindowsCamera.exe",
                "maps": "Maps.exe",
                "mail": "HxOutlook.exe",
                "calendar": "HxCalendarAppImm.exe",
                "store": "WinStore.App.exe",
            }
            title_lower = title.lower()
            proc_name = _UWP_PROCESS_MAP.get(title_lower)
            if not proc_name:
                # Generic: try "title.exe" pattern
                for key, val in _UWP_PROCESS_MAP.items():
                    if key in title_lower:
                        proc_name = val
                        break

            if proc_name:
                import subprocess
                subprocess.run(
                    ["taskkill", "/F", "/IM", proc_name],
                    capture_output=True, timeout=5)
            else:
                # Generic fallback: Alt+F4 on focused window
                import pyautogui
                remaining = gw.getWindowsWithTitle(title)
                if remaining:
                    try:
                        remaining[0].activate()
                        import time
                        time.sleep(0.3)
                        pyautogui.hotkey("alt", "F4")
                    except Exception:
                        pass

            # Final verification
            import time
            time.sleep(0.5)
            still_open = gw.getWindowsWithTitle(title)
            if not still_open:
                return f"Done, I've closed {title}."
            return f"I sent the close signal to {title}, but it may still be open."

        return f"I couldn't find a window called {title}. Is it running?"
    except Exception as e:
        logging.error(f"Error closing window: {e}")
        return f"I tried to close {title} but something went wrong."


# --- System commands (native Windows, no .bat files needed) ---

def shutdown_computer():
    """Initiate system shutdown with 60s grace period."""
    os.system("shutdown /s /t 60")
    return "Alright, your computer will shut down in 60 seconds. Say 'cancel shutdown' if you change your mind."


def restart_computer():
    """Initiate system restart with 60s grace period."""
    os.system("shutdown /r /t 60")
    return "Got it, restarting in 60 seconds. Say 'cancel shutdown' if you want to stop it."


def cancel_shutdown():
    """Cancel a pending shutdown or restart."""
    os.system("shutdown /a")
    return "I've cancelled the shutdown. You're good to go."


def sleep_computer():
    """Put computer to sleep."""
    os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    return "Putting your computer to sleep now. Goodnight!"


