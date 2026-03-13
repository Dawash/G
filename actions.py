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
    """Close windows matching the given title, with event-driven verification."""
    try:
        windows = gw.getWindowsWithTitle(title)
        if windows:
            for w in windows:
                w.close()
            # Verify window actually closed instead of assuming
            try:
                from automation.event_waiter import wait_for_window_gone
                result = wait_for_window_gone(title, max_wait=5, interval=0.2)
                if result["gone"]:
                    return f"Done, I've closed {title}."
                else:
                    return f"I sent the close signal to {title}, but it may still be open (save dialog?)."
            except ImportError:
                return f"Done, I've closed {title}."
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


