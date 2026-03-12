"""
Intelligent Windows application discovery and launching.

Combines three sources for maximum coverage:
  1. Registry App Paths (fast, direct exe paths)
  2. Start Menu .lnk shortcuts (what users see)
  3. Registry Uninstall keys (comprehensive metadata)

Results are cached to disk and memory for instant repeat lookups.
Fuzzy matching handles voice input variations like "VS Code" -> "Visual Studio Code".
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import subprocess
import time
import winreg
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_cache.json")
CACHE_MAX_AGE = 86400  # Rebuild once per day

# Common voice aliases -> canonical app names
ALIASES = {
    "vs code": "visual studio code",
    "code editor": "visual studio code",
    "code": "visual studio code",
    "chrome": "google chrome",
    "firefox": "mozilla firefox",
    "edge": "microsoft edge",
    "explorer": "file explorer",
    "files": "file explorer",
    "cmd": "command prompt",
    "terminal": "windows terminal",
    "powershell": "windows powershell",
    "word": "microsoft word",
    "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint",
    "outlook": "microsoft outlook",
    "teams": "microsoft teams",
    "discord": "discord",
    "slack": "slack",
    "steam": "steam",
    "obs": "obs studio",
}

# Common Windows apps with direct exe paths — avoids "Select an app" popups
# These are checked BEFORE protocol URIs to prevent file-association dialogs
DIRECT_EXE_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "wordpad": "write.exe",
    "snipping tool": "snippingtool.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "device manager": "devmgmt.msc",
    "disk management": "diskmgmt.msc",
    "event viewer": "eventvwr.msc",
    "regedit": "regedit.exe",
    "registry editor": "regedit.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "windows terminal": "wt.exe",
    "terminal": "wt.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
}

# UWP / built-in Windows apps using protocol URIs
WINDOWS_PROTOCOL_APPS = {
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "system settings": "ms-settings:",
    "bluetooth": "ms-settings:bluetooth",
    "bluetooth settings": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "wifi settings": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "network settings": "ms-settings:network",
    "display settings": "ms-settings:display",
    "sound settings": "ms-settings:sound",
    "windows update": "ms-settings:windowsupdate",
    "update": "ms-settings:windowsupdate",
    "personalization": "ms-settings:personalization",
    "wallpaper": "ms-settings:personalization-background",
    "background": "ms-settings:personalization-background",
    "photos": "ms-photos:",
    "calculator": "calculator:",
    "calendar": "outlookcal:",
    "store": "ms-windows-store:",
    "maps": "bingmaps:",
    "mail": "outlookmail:",
    "camera": "microsoft.windows.camera:",
    "clock": "ms-clock:",
    "xbox": "xbox:",
    "spotify": "spotify:",
    "whatsapp desktop": "whatsapp:",
}

# Common websites — opened in default browser if no desktop app found
WEB_SHORTCUTS = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "google maps": "https://maps.google.com",
    "google drive": "https://drive.google.com",
    "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "google photos": "https://photos.google.com",
    "google calendar": "https://calendar.google.com",
    "google translate": "https://translate.google.com",
    "netflix": "https://www.netflix.com",
    "twitter": "https://twitter.com",
    "x": "https://twitter.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "reddit": "https://www.reddit.com",
    "github": "https://github.com",
    "linkedin": "https://www.linkedin.com",
    "twitch": "https://www.twitch.tv",
    "amazon": "https://www.amazon.com",
    "wikipedia": "https://www.wikipedia.org",
    "chatgpt": "https://chat.openai.com",
    "whatsapp": "https://web.whatsapp.com",
    "tiktok": "https://www.tiktok.com",
    "pinterest": "https://www.pinterest.com",
    "spotify web": "https://open.spotify.com",
    "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com",
    "notion": "https://www.notion.so",
    "figma": "https://www.figma.com",
    "canva": "https://www.canva.com",
    "zoom": "https://zoom.us",
    "dropbox": "https://www.dropbox.com",
    "ebay": "https://www.ebay.com",
    "disney plus": "https://www.disneyplus.com",
    "hulu": "https://www.hulu.com",
    "prime video": "https://www.primevideo.com",
    "soundcloud": "https://soundcloud.com",
    "bing": "https://www.bing.com",
    "hacker news": "https://news.ycombinator.com",
}


# ===================================================================
# Registry scanning: App Paths
# ===================================================================

def _scan_app_paths():
    """Read HKLM/HKCU App Paths registry keys for direct exe paths."""
    results = {}
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ)
        except OSError:
            continue

        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        exe_path = winreg.QueryValueEx(subkey, "")[0].strip('"')
                        if exe_path and os.path.isfile(exe_path):
                            name = os.path.splitext(subkey_name)[0].replace("_", " ").replace("-", " ")
                            results[name.lower()] = {"name": name, "exe_path": exe_path, "source": "app_paths"}
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)

    return results


# ===================================================================
# Start Menu .lnk scanning
# ===================================================================

def _resolve_lnk(lnk_path):
    """Resolve a .lnk shortcut to its target exe path."""
    try:
        import win32com.client
        ws = win32com.client.Dispatch("WScript.Shell")
        shortcut = ws.CreateShortCut(lnk_path)
        target = shortcut.Targetpath
        if target and target.lower().endswith(".exe") and os.path.isfile(target):
            return target
    except Exception:
        pass
    return None


def _scan_start_menu():
    """Walk Start Menu folders and resolve .lnk files to exe paths."""
    results = {}
    start_dirs = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ]

    seen_paths = set()
    for start_dir in start_dirs:
        if not os.path.isdir(start_dir):
            continue
        for root, _, files in os.walk(start_dir):
            for fname in files:
                if not fname.lower().endswith(".lnk"):
                    continue
                lnk_path = os.path.join(root, fname)
                target = _resolve_lnk(lnk_path)
                if target and target.lower() not in seen_paths:
                    seen_paths.add(target.lower())
                    display_name = os.path.splitext(fname)[0]
                    results[display_name.lower()] = {
                        "name": display_name,
                        "exe_path": target,
                        "source": "start_menu",
                    }

    return results


# ===================================================================
# Uninstall registry scanning
# ===================================================================

def _scan_uninstall_registry():
    """Read Uninstall keys for installed app metadata."""
    results = {}
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
         winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
         winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
         winreg.KEY_READ),
    ]

    for hive, path, flags in registry_paths:
        try:
            key = winreg.OpenKey(hive, path, 0, flags)
        except OSError:
            continue

        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name, 0, flags) as subkey:
                        try:
                            display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        except OSError:
                            continue

                        if not display_name:
                            continue

                        key_lower = display_name.lower()
                        if key_lower in results:
                            continue

                        exe_path = _find_exe_from_uninstall(subkey)
                        if exe_path:
                            results[key_lower] = {
                                "name": display_name,
                                "exe_path": exe_path,
                                "source": "uninstall",
                            }
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)

    return results


def _find_exe_from_uninstall(subkey):
    """Extract an exe path from an Uninstall registry entry."""
    # Try DisplayIcon first (often points to main exe)
    try:
        icon = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
        icon = icon.strip('"').split(",")[0]
        if icon.lower().endswith(".exe") and os.path.isfile(icon):
            return icon
    except OSError:
        pass

    # Try InstallLocation
    try:
        loc = winreg.QueryValueEx(subkey, "InstallLocation")[0].strip('"').rstrip("\\")
        if loc and os.path.isdir(loc):
            for item in os.listdir(loc):
                if item.lower().endswith(".exe"):
                    full = os.path.join(loc, item)
                    if os.path.isfile(full):
                        return full
    except OSError:
        pass

    return None


# ===================================================================
# Cache management
# ===================================================================

_app_index = None


def _build_index():
    """Scan all sources and build a unified app index."""
    t0 = time.perf_counter()
    index = {}

    # Lower priority first — higher priority overwrites
    index.update(_scan_uninstall_registry())
    index.update(_scan_app_paths())
    index.update(_scan_start_menu())

    elapsed = time.perf_counter() - t0
    logger.info(f"App index: {len(index)} apps discovered in {elapsed:.2f}s")
    return index


def _load_cache():
    """Load app index from disk cache if fresh."""
    if not os.path.isfile(CACHE_FILE):
        return None
    try:
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age > CACHE_MAX_AGE:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(index):
    """Save app index to disk."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=1)
    except OSError as e:
        logger.warning(f"Could not save app cache: {e}")


def get_app_index(force_refresh=False):
    """Get the app index, using memory/disk cache when available."""
    global _app_index

    if _app_index is not None and not force_refresh:
        return _app_index

    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            _app_index = cached
            return _app_index

    _app_index = _build_index()
    _save_cache(_app_index)
    return _app_index


# ===================================================================
# Fuzzy matching
# ===================================================================

def find_best_match(query, score_cutoff=65):
    """Find the best matching app using exact match, aliases, then fuzzy match."""
    index = get_app_index()
    query_lower = query.lower().strip()

    # Exact match
    if query_lower in index:
        return index[query_lower]

    # Alias lookup
    alias_target = ALIASES.get(query_lower)
    if alias_target and alias_target in index:
        return index[alias_target]

    # Fuzzy match
    try:
        from rapidfuzz import process, fuzz
        choices = list(index.keys())
        result = process.extractOne(query_lower, choices, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
        if result:
            matched_name, score, _ = result
            logger.info(f"Fuzzy matched '{query}' -> '{matched_name}' (score={score:.1f})")
            return index[matched_name]
    except ImportError:
        # Fallback: simple substring match
        for key, entry in index.items():
            if query_lower in key or key in query_lower:
                return entry

    return None


def find_similar_apps(name, limit=3):
    """Find app names similar to `name` for 'did you mean?' suggestions."""
    from difflib import get_close_matches
    index = get_app_index()
    if not index:
        return []
    app_names = list(index.keys())
    matches = get_close_matches(name.lower(), app_names, n=limit, cutoff=0.4)
    # Capitalize nicely
    return [m.title() for m in matches]


# ===================================================================
# Launch application (main entry point)
# ===================================================================

def launch_app(app_name):
    """
    Find and launch an application by name.
    Tries in order: UWP protocol → web shortcuts → installed apps → browser fallback.
    Web shortcuts are checked BEFORE fuzzy-matching to prevent misrouting
    (e.g. "Facebook" fuzzy-matching to "Microsoft Outlook").
    """
    import webbrowser
    name_lower = app_name.lower().strip()

    # 0. Direct exe apps — fast, no popups, no protocol URI issues
    if name_lower in DIRECT_EXE_APPS:
        exe = DIRECT_EXE_APPS[name_lower]
        try:
            # .msc files need os.startfile() or mmc.exe, not subprocess.Popen
            if exe.endswith('.msc'):
                os.startfile(exe)
            else:
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(
                    [exe], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
            logger.info(f"Launched direct exe '{name_lower}' -> {exe}")
            _activate_app_window(app_name, name_lower)
            return f"Sure, I've opened {app_name} for you."
        except OSError:
            logger.debug(f"Direct exe '{exe}' failed, falling through")
            # Fall through to protocol/registry search

    # 1. UWP / protocol apps (Settings, Calculator, etc.)
    if name_lower in WINDOWS_PROTOCOL_APPS:
        try:
            os.startfile(WINDOWS_PROTOCOL_APPS[name_lower])
            _activate_app_window(app_name, name_lower, timeout=4.0)
            return f"Sure, opening {app_name} for you."
        except OSError as e:
            logger.error(f"Failed to open protocol app '{app_name}': {e}")
            # Fall through to try installed apps / web shortcuts instead of giving up

    # 2. Web shortcuts FIRST — prevents "Facebook" -> "Microsoft Outlook" misroute
    if name_lower in WEB_SHORTCUTS:
        url = WEB_SHORTCUTS[name_lower]
        webbrowser.open(url)
        logger.info(f"Opened web shortcut '{name_lower}' -> {url}")
        return f"Sure, I've opened {app_name} in your browser."

    # 2b. Fuzzy web shortcut match — exact word match only (no substring)
    for web_name, url in WEB_SHORTCUTS.items():
        # Only match if the names are close (not just substring containment)
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, name_lower, web_name).ratio()
        if ratio >= 0.8:
            webbrowser.open(url)
            logger.info(f"Fuzzy web match '{name_lower}' -> {web_name} ({url}), ratio={ratio:.2f}")
            return f"I've opened {web_name} in your browser for you."

    # 3. Search installed apps (registry + start menu + fuzzy match)
    match = find_best_match(app_name)
    if match:
        exe_path = match["exe_path"]
        display_name = match["name"]

        # Block known bad fuzzy matches (SDK installers, setup.exe, etc.)
        exe_basename = os.path.basename(exe_path).lower()
        bad_patterns = ["setup.exe", "uninstall", "winsdksetup", "installer",
                        "unins000", "update.exe"]
        if any(p in exe_basename for p in bad_patterns):
            logger.warning(f"Blocked bad match: '{app_name}' -> '{display_name}' ({exe_path})")
            # Don't launch installers/SDK setup as regular apps
        else:
            # Verify exe still exists
            if not os.path.isfile(exe_path):
                logger.info(f"Cached path gone, refreshing: {exe_path}")
                get_app_index(force_refresh=True)
                match = find_best_match(app_name)
                if not match or not os.path.isfile(match["exe_path"]):
                    return f"I found {display_name} earlier but it seems to have been moved or uninstalled."
                exe_path = match["exe_path"]
                display_name = match["name"]

            try:
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(
                    [exe_path],
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
                logger.info(f"Launched '{display_name}' from {exe_path}")
                # Wait briefly and bring the new window to foreground
                _activate_app_window(display_name, name_lower)
                return f"Sure, I've opened {display_name} for you."
            except OSError as e:
                logger.error(f"Failed to launch '{display_name}': {e}")
                return f"I found {display_name} but couldn't start it. It might be locked."

    return f"I couldn't find {app_name} on your computer. Is it installed?"


def _activate_app_window(display_name, name_lower, timeout=5.0):
    """Wait for a newly launched app window to appear and bring it to foreground.

    Uses multiple strategies:
    1. pygetwindow.activate() (fast, works 80% of the time)
    2. ctypes SetForegroundWindow (reliable Win32 API fallback)
    3. Alt-key trick to bypass foreground lock restriction
    Also detects and dismisses "Select an app" / "Open with" popups.
    """
    import time
    try:
        import pygetwindow as gw
    except ImportError:
        return

    # Keywords to search for in window titles
    keywords = [name_lower]
    for word in display_name.lower().split():
        if len(word) > 2 and word not in ("the", "for", "and"):
            keywords.append(word)

    def _force_foreground(hwnd):
        """Bring window to foreground using Win32 API (more reliable than pygetwindow)."""
        try:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            # Check if minimized and restore
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.2)
            # Alt-key trick: press and release Alt to bypass the foreground lock
            # Windows prevents SetForegroundWindow unless the calling process is foreground
            user32.keybd_event(0x12, 0, 0, 0)  # Alt down
            user32.keybd_event(0x12, 0, 2, 0)  # Alt up
            time.sleep(0.05)
            result = user32.SetForegroundWindow(hwnd)
            if not result:
                # Second attempt: use BringWindowToTop
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            return True
        except Exception as e:
            logger.debug(f"_force_foreground failed: {e}")
            return False

    def _dismiss_app_picker():
        """Detect and dismiss 'Select an app' / 'Open with' popups."""
        try:
            active = gw.getActiveWindow()
            if active and active.title:
                title_lower = active.title.lower()
                picker_keywords = [
                    "select an app", "open with", "how do you want to open",
                    "choose an app", "choose default", "windows cannot find",
                    "look for an app", "select a default",
                ]
                if any(kw in title_lower for kw in picker_keywords):
                    logger.info(f"Detected app picker popup: '{active.title}', dismissing")
                    import pyautogui
                    pyautogui.press("escape")
                    time.sleep(0.5)
                    return True
        except Exception:
            pass
        return False

    start = time.time()
    found_window = False
    while time.time() - start < timeout:
        time.sleep(0.4)

        # Check for blocking popups first
        _dismiss_app_picker()

        try:
            for w in gw.getAllWindows():
                if not w.title:
                    continue
                title_lower = w.title.lower()
                for kw in keywords:
                    if kw in title_lower:
                        found_window = True
                        try:
                            # First try pygetwindow
                            if w.isMinimized:
                                w.restore()
                                time.sleep(0.2)
                            w.activate()
                            logger.info(f"Activated window: {w.title}")
                            return
                        except Exception:
                            # Fallback: Win32 API force foreground
                            hwnd = w._hWnd
                            if _force_foreground(hwnd):
                                logger.info(f"Force-activated window: {w.title}")
                                return
        except Exception:
            pass

    # If we found the window but couldn't activate it, try one more Win32 approach
    if found_window:
        try:
            for w in gw.getAllWindows():
                if not w.title:
                    continue
                title_lower = w.title.lower()
                for kw in keywords:
                    if kw in title_lower:
                        _force_foreground(w._hWnd)
                        return
        except Exception:
            pass
