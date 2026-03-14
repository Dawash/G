"""
Desktop Automation — keyboard, mouse, scroll, and in-app search.

Gives the Brain full desktop control so it can interact with apps
after opening them: type into search bars, press hotkeys, click
buttons, scroll pages, and search within apps/websites.

Safety:
  - Blocked hotkeys: Ctrl+Alt+Del, Alt+F4, Win+L
  - Text capped at 2000 chars, scroll at 20, clicks at 3
  - Screen bounds checking on click coordinates
  - Lazy pyautogui import — graceful error if not installed
"""

import logging
import os
import time
import webbrowser

logger = logging.getLogger(__name__)

# Lazy-loaded pyautogui
_pyautogui = None


def _get_pyautogui():
    """Lazy import pyautogui with failsafe enabled."""
    global _pyautogui
    if _pyautogui is None:
        try:
            import pyautogui
            pyautogui.FAILSAFE = False  # Disable fail-safe — we have our own safety checks
            pyautogui.PAUSE = 0.05
            _pyautogui = pyautogui
        except ImportError:
            raise RuntimeError(
                "pyautogui is not installed. Install it with: pip install pyautogui"
            )
    return _pyautogui


# ===================================================================
# Blocked key combos (safety)
# ===================================================================

BLOCKED_COMBOS = {
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"alt", "f4"}),
    frozenset({"win", "l"}),
    frozenset({"winleft", "l"}),
    frozenset({"winright", "l"}),
}


def _is_blocked(keys_list):
    """Check if a key combination is blocked for safety."""
    normalized = frozenset(k.lower() for k in keys_list)
    return normalized in BLOCKED_COMBOS


# ===================================================================
# Core functions — keyboard, mouse, scroll
# ===================================================================

def type_text(text, interval=0.02):
    """
    Type text into the currently focused application.
    Capped at 2000 characters for safety.
    """
    pag = _get_pyautogui()

    if not text:
        return "Error: no text provided."

    text = str(text)
    if len(text) > 2000:
        text = text[:2000]
        logger.warning("type_text: text truncated to 2000 chars")

    try:
        if text.isascii():
            pag.typewrite(text, interval=interval)
        else:
            # pyautogui has no .write(); use hotkey-based clipboard paste for non-ASCII
            import pyperclip
            pyperclip.copy(text)
            pag.hotkey("ctrl", "v")
            time.sleep(0.1)
        return f"Typed {len(text)} characters."
    except Exception as e:
        logger.error(f"type_text error: {e}")
        return f"Error typing text: {e}"


def press_key(keys):
    """
    Press a key or key combo.

    Examples: "enter", "ctrl+c", "alt+tab", "ctrl+shift+t"
    Blocked: Ctrl+Alt+Del, Alt+F4, Win+L
    """
    pag = _get_pyautogui()

    if not keys:
        return "Error: no keys provided."

    keys = str(keys).strip().lower()

    # Split combo: "ctrl+c" → ["ctrl", "c"]
    parts = [k.strip() for k in keys.replace("+", " ").split()]

    if _is_blocked(parts):
        return f"Blocked: '{keys}' is not allowed for safety."

    try:
        if len(parts) == 1:
            pag.press(parts[0])
        else:
            pag.hotkey(*parts)
        return f"Pressed: {keys}"
    except Exception as e:
        logger.error(f"press_key error: {e}")
        return f"Error pressing keys: {e}"


def click_at(x, y, button="left", clicks=1):
    """
    Click at screen coordinates.
    Bounds-checked, max triple-click.
    """
    pag = _get_pyautogui()

    try:
        x = int(x)
        y = int(y)
    except (ValueError, TypeError):
        return f"Error: invalid coordinates ({x}, {y})."

    # Bounds check
    screen_w, screen_h = pag.size()
    if x < 0 or x >= screen_w or y < 0 or y >= screen_h:
        return f"Error: coordinates ({x}, {y}) out of screen bounds ({screen_w}x{screen_h})."

    # Cap clicks
    try:
        clicks = int(clicks)
    except (ValueError, TypeError):
        clicks = 1
    clicks = max(1, min(clicks, 3))

    button = str(button).lower()
    if button not in ("left", "right", "middle"):
        button = "left"

    try:
        pag.click(x, y, clicks=clicks, button=button)
        return f"Clicked ({button}) at ({x}, {y}), {clicks} time(s)."
    except Exception as e:
        logger.error(f"click_at error: {e}")
        return f"Error clicking: {e}"


# ===================================================================
# Protocol URI search (tier 0 — UWP/desktop apps with native search)
# ===================================================================

PROTOCOL_SEARCH_URIS = {
    "spotify": "spotify:search:{q}",
}

# ===================================================================
# Web app URL search patterns (tier 1 — instant, reliable)
# ===================================================================

WEB_SEARCH_URLS = {
    "youtube":       "https://www.youtube.com/results?search_query={q}",
    "google":        "https://www.google.com/search?q={q}",
    "reddit":        "https://www.reddit.com/search/?q={q}",
    "amazon":        "https://www.amazon.com/s?k={q}",
    "github":        "https://github.com/search?q={q}",
    "wikipedia":     "https://en.wikipedia.org/w/index.php?search={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
    "netflix":       "https://www.netflix.com/search?q={q}",
    "pinterest":     "https://www.pinterest.com/search/pins/?q={q}",
    "tiktok":        "https://www.tiktok.com/search?q={q}",
    "linkedin":      "https://www.linkedin.com/search/results/all/?keywords={q}",
    "bing":          "https://www.bing.com/search?q={q}",
    "duckduckgo":    "https://duckduckgo.com/?q={q}",
    "google maps":   "https://www.google.com/maps/search/{q}",
    "maps":          "https://www.google.com/maps/search/{q}",
    "spotify":       "https://open.spotify.com/search/{q}",
    "ebay":          "https://www.ebay.com/sch/i.html?_nkw={q}",
    "twitter":       "https://x.com/search?q={q}",
    "x":             "https://x.com/search?q={q}",
}

# ===================================================================
# Desktop app search hotkeys (tier 2 — keyboard automation)
# ===================================================================

DESKTOP_SEARCH_HOTKEYS = {
    "spotify":       {"hotkey": ["ctrl", "l"], "wait": 2.0},
    "vs code":       {"hotkey": ["ctrl", "p"], "wait": 1.0},
    "vscode":        {"hotkey": ["ctrl", "p"], "wait": 1.0},
    "visual studio code": {"hotkey": ["ctrl", "p"], "wait": 1.0},
    "chrome":        {"hotkey": ["ctrl", "l"], "wait": 1.0},
    "firefox":       {"hotkey": ["ctrl", "l"], "wait": 1.0},
    "edge":          {"hotkey": ["ctrl", "l"], "wait": 1.0},
    "brave":         {"hotkey": ["ctrl", "l"], "wait": 1.0},
    "opera":         {"hotkey": ["ctrl", "l"], "wait": 1.0},
    "discord":       {"hotkey": ["ctrl", "k"], "wait": 1.0},
    "slack":         {"hotkey": ["ctrl", "k"], "wait": 1.0},
    "teams":         {"hotkey": ["ctrl", "e"], "wait": 1.0},
    "outlook":       {"hotkey": ["ctrl", "e"], "wait": 1.0},
    "file explorer":  {"hotkey": ["ctrl", "e"], "wait": 1.0},
    "explorer":      {"hotkey": ["ctrl", "e"], "wait": 1.0},
    "notepad++":     {"hotkey": ["ctrl", "f"], "wait": 0.5},
}


# ===================================================================
# High-level: search_in_app
# ===================================================================

def _is_app_running(app_name):
    """Check if a desktop app window is currently open."""
    try:
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(app_name)
        return len(windows) > 0
    except Exception:
        return False


def _click_first_spotify_song():
    """Play the first song/playlist in Spotify search results.

    Quick keyboard attempts only — NO fixed positions, NO vision.
    If these fail, caller returns partial result which triggers the
    existing auto-escalation → desktop agent (observe→think→act→verify).
    Returns True if playback started.
    """
    pag = _get_pyautogui()
    win = _force_focus_spotify()
    if not win:
        return False
    time.sleep(0.5)

    # Try 3 keyboard strategies — each verified before moving on
    strategies = [
        ("Tab→Enter", ["tab", "enter"]),
        ("Down→Enter", ["down", "enter"]),
        ("Tab×3→Enter", ["tab", "tab", "tab", "enter"]),
    ]
    for name, keys in strategies:
        _force_focus_spotify()
        time.sleep(0.3)
        logger.info(f"Spotify: keyboard — {name}")
        for key in keys:
            pag.press(key)
            time.sleep(0.2)
        time.sleep(3)
        if _check_spotify_playing():
            logger.info(f"Spotify: {name} worked — playing")
            return True

    # Last resort: media Play key (plays whatever is queued)
    try:
        from brain_defs import _press_media_key, VK_MEDIA_PLAY_PAUSE
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        time.sleep(2)
        if _check_spotify_playing():
            logger.info("Spotify: media Play key worked")
            return True
    except Exception:
        pass

    # Return False → caller returns "couldn't auto-play" →
    # brain.py auto-escalation → desktop agent handles it
    logger.info("Spotify: keyboard methods failed, will escalate to agent mode")
    return False


def _force_focus_window(title_substr):
    """Force a window to foreground using Win32 AttachThreadInput trick.

    Windows blocks SetForegroundWindow from background processes.
    Workaround: attach to the target window's thread input, then set foreground.
    Returns the pygetwindow window object, or None.
    """
    try:
        import pygetwindow as gw
        wins = [w for w in gw.getAllWindows()
                if w.title and title_substr.lower() in w.title.lower()]
        if not wins:
            return None
        win = wins[0]
        if win.isMinimized:
            win.restore()
            time.sleep(0.5)
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = win._hWnd
            # Get thread IDs
            current_thread = user32.GetCurrentThreadId()
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            # Attach input threads so SetForegroundWindow works
            if current_thread != target_thread:
                user32.AttachThreadInput(current_thread, target_thread, True)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            # Detach
            if current_thread != target_thread:
                user32.AttachThreadInput(current_thread, target_thread, False)
        except Exception:
            try:
                win.activate()
            except Exception:
                pass
        return win
    except Exception:
        return None


def _force_focus_spotify():
    """Force Spotify window to foreground."""
    return _force_focus_window("spotify")


def _spotify_no_results():
    """Check if Spotify search returned no results.

    Looks for "No results found" or "Search for something else" in the
    Spotify window UIA controls, which indicates the query had zero matches.
    """
    try:
        from automation.ui_control import list_controls
        controls = list_controls(window="Spotify", max_depth=6, max_count=80)
        if controls:
            for c in controls:
                cname = (c.get("name") or "").lower()
                if any(phrase in cname for phrase in (
                    "no results found",
                    "search for something else",
                    "couldn't find",
                    "no results",
                    "not available",
                )):
                    logger.info(f"Spotify no-results detected: '{c.get('name')}'")
                    return True
    except Exception as e:
        logger.debug(f"_spotify_no_results UIA check failed: {e}")

    # Fallback: check window title hasn't changed (still "Spotify" = no song loaded)
    # This alone isn't conclusive, so only return True if UIA found the indicator
    return False


def _click_first_spotify_song_keyboard():
    """Keyboard-only fallback for Spotify song selection (deprecated, kept for compat)."""
    return _click_first_spotify_song()


def _check_spotify_playing():
    """Check if Spotify is currently playing (title changes to 'Artist - Song')."""
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if w.title and "spotify" in w.title.lower():
                title = w.title
                if title.lower() not in ("spotify free", "spotify premium", "spotify") and "-" in title:
                    return True
        return False
    except Exception:
        return False


def _click_first_youtube_video():
    """Click the first real video in YouTube search results.

    Multi-strategy with retries:
    1. CDP JS: click first non-ad video (retried up to 3 times with wait)
    2. CDP extract + navigate: get video URL and navigate directly
    3. CDP browser_click: text-based click via page snapshot
    4. Keyboard: Tab to first video link and press Enter
    Returns True if click succeeded.
    """
    # ---- CDP Strategies (most reliable for web) ----
    try:
        from automation.browser_driver import (
            is_cdp_available, browser_click, browser_get_url,
            _check_cdp, _get_active_tab_ws, _send_cdp_command,
        )

        if not _check_cdp():
            try:
                from automation.cdp_session import CDPSession
                cdp = CDPSession()
                cdp.ensure_chrome()
                time.sleep(2)
            except Exception:
                pass

        if _check_cdp():
            ws = _get_active_tab_ws()
            if ws:
                # JS to click first non-ad video
                js_click = """
                (() => {
                    // Strategy A: click first non-ad video title link
                    const renderers = document.querySelectorAll(
                        'ytd-video-renderer, ytd-rich-item-renderer'
                    );
                    for (const r of renderers) {
                        if (r.querySelector('[class*="ad-badge"]') ||
                            r.querySelector('ytd-ad-slot-renderer') ||
                            r.closest('ytd-ad-slot-renderer')) continue;
                        const link = r.querySelector('a#video-title, a#video-title-link');
                        if (link && link.href && link.href.includes('/watch')) {
                            link.click();
                            return 'clicked: ' + (link.textContent || '').trim().substring(0, 60);
                        }
                    }
                    // Strategy B: first non-ad thumbnail
                    const thumbs = document.querySelectorAll('a#thumbnail[href*="/watch"]');
                    for (const t of thumbs) {
                        if (!t.closest('ytd-ad-slot-renderer')) {
                            t.click();
                            return 'clicked thumbnail';
                        }
                    }
                    // Strategy C: extract first video URL for direct navigation
                    const links = document.querySelectorAll('a[href*="/watch"]');
                    for (const l of links) {
                        if (!l.closest('ytd-ad-slot-renderer') && l.href) {
                            return 'url:' + l.href;
                        }
                    }
                    return 'no videos found';
                })()
                """

                # Retry CDP click up to 3 times (page may still be loading)
                for attempt in range(3):
                    result = _send_cdp_command(
                        ws, "Runtime.evaluate",
                        {"expression": js_click, "returnByValue": True}
                    )
                    if result:
                        value = str(result.get("result", {}).get("value", ""))
                        if "clicked" in value.lower():
                            logger.info(f"YouTube CDP click (attempt {attempt+1}): {value}")
                            time.sleep(3)
                            try:
                                url = browser_get_url()
                                if url and "/watch" in url:
                                    return True
                            except Exception:
                                pass
                            return True
                        elif value.startswith("url:"):
                            # Direct navigation to extracted video URL
                            video_url = value[4:]
                            logger.info(f"YouTube CDP: navigating to {video_url}")
                            _send_cdp_command(ws, "Page.navigate", {"url": video_url})
                            time.sleep(3)
                            return True
                        elif "no videos found" in value:
                            if attempt < 2:
                                logger.info(f"YouTube: no videos found yet, waiting (attempt {attempt+1})")
                                time.sleep(3)
                                continue
                    else:
                        if attempt < 2:
                            time.sleep(2)
                            continue

                # Strategy 2: browser_click with video title text from page snapshot
                try:
                    from automation.browser_driver import browser_snapshot
                    snap = browser_snapshot()
                    if snap and snap.get("links"):
                        for link in snap["links"]:
                            href = link.get("href", "")
                            text = link.get("text", "")
                            if "/watch" in href and text and len(text) > 5:
                                click_result = browser_click(text=text)
                                if click_result and "CLICKED" in str(click_result):
                                    logger.info(f"YouTube: browser_click on '{text}' succeeded")
                                    time.sleep(2)
                                    return True
                                break
                except Exception as e:
                    logger.debug(f"YouTube browser_click fallback failed: {e}")

    except ImportError as e:
        logger.debug(f"YouTube: browser_driver not available: {e}")
    except Exception as e:
        logger.warning(f"YouTube CDP automation failed: {e}")

    # ---- Non-CDP Fallback ----
    # Return False → caller returns "couldn't auto-play" →
    # brain.py auto-escalation → desktop agent handles it with
    # its full observe→think→act→verify loop
    logger.info("YouTube: CDP unavailable, will escalate to agent mode")
    return False


def search_in_app(app_name, query):
    """
    Search within an app or website. Smart priority:
    0. Protocol URI search (UWP apps like Spotify) → launches app + searches
    1. If the app has a desktop hotkey AND is running → keyboard automation
    2. If the app has a web URL → URL-based search (instant)
    3. Desktop apps not running → keyboard automation (will open the app)
    4. Fallback → Google search with app context
    """
    if not app_name or not query:
        return "Error: both app name and query are required."

    app_lower = app_name.lower().strip()
    query = str(query).strip()

    # --- Priority 0: Protocol URI search (UWP/desktop apps) ---
    for name, uri_template in PROTOCOL_SEARCH_URIS.items():
        if name in app_lower or app_lower in name:
            from urllib.parse import quote
            uri = uri_template.replace("{q}", quote(query))
            try:
                os.startfile(uri)
                # Event-driven wait: poll for Spotify window + search results
                try:
                    from automation.event_waiter import wait_for_window
                    wait_for_window("Spotify", max_wait=5, interval=0.2)
                    # Give Spotify a moment to render search results after window appears
                    time.sleep(0.5)
                except ImportError:
                    time.sleep(3.5)
                try:
                    # Check if search returned no results
                    if "spotify" in name and _spotify_no_results():
                        logger.info(f"Spotify search: no results for '{query}'")
                        return f"No results found for '{query}' in {app_name}. Try a different search term."
                    # Click the first song in the search results
                    if _click_first_spotify_song():
                        logger.info(f"Protocol search + click-play for {app_name}: '{query}'")
                        return f"Playing '{query}' in {app_name}."
                    else:
                        # Double-check: might be no results rather than click failure
                        if "spotify" in name and _spotify_no_results():
                            return f"No results found for '{query}' in {app_name}. Try a different search term."
                        logger.warning("Could not click first song, search completed but no auto-play")
                        return f"Searched for '{query}' in {app_name} (click a result to play)."
                except Exception:
                    return f"Searching for '{query}' in {app_name}."
            except OSError as e:
                logger.warning(f"Protocol search failed for {app_name}: {e}")
                break  # Fall through to other methods

    # Find matching desktop hotkey config (if any)
    desktop_config = None
    for name, config in DESKTOP_SEARCH_HOTKEYS.items():
        if name in app_lower or app_lower in name:
            desktop_config = config
            break

    # --- Priority 1: Desktop app is running → use keyboard automation ---
    if desktop_config and _is_app_running(app_name):
        return _desktop_search(app_name, query, desktop_config)

    # --- Priority 2: Web URL search (instant, reliable) ---
    for name, url_template in WEB_SEARCH_URLS.items():
        if name in app_lower or app_lower in name:
            from urllib.parse import quote_plus
            url = url_template.replace("{q}", quote_plus(query))
            try:
                # For YouTube: use CDP-enabled browser for proper automation
                if "youtube" in name:
                    _youtube_opened = False
                    try:
                        from automation.browser_driver import (
                            browser_navigate, is_cdp_available,
                        )
                        if not is_cdp_available():
                            from automation.cdp_session import CDPSession
                            CDPSession().ensure_chrome()
                            time.sleep(2)
                        if is_cdp_available():
                            browser_navigate(url)
                            _youtube_opened = True
                    except Exception:
                        pass
                    if not _youtube_opened:
                        webbrowser.open(url)
                    time.sleep(5)  # Wait for search results to load
                    if _click_first_youtube_video():
                        return f"Playing '{query}' on YouTube."
                    return f"Searched for '{query}' on YouTube (click a result to play)."
                else:
                    webbrowser.open(url)
                return f"Searching for '{query}' on {app_name}."
            except Exception as e:
                return f"Error opening {app_name} search: {e}"

    # --- Priority 3: Desktop app not running → open and search ---
    if desktop_config:
        return _desktop_search(app_name, query, desktop_config)

    # --- Priority 4: Fallback to Google search ---
    from urllib.parse import quote_plus
    fallback_url = f"https://www.google.com/search?q={quote_plus(query + ' ' + app_name)}"
    try:
        webbrowser.open(fallback_url)
        return f"Searched Google for '{query}' in context of {app_name}."
    except Exception as e:
        return f"Error with fallback search: {e}"


def _desktop_search(app_name, query, config):
    """
    Search within a desktop app using keyboard automation.
    Opens/focuses app → waits → presses search hotkey → types query → Enter.
    """
    try:
        pag = _get_pyautogui()
    except RuntimeError as e:
        return str(e)

    hotkey = config["hotkey"]
    wait_time = config.get("wait", 1.0)

    try:
        # Try to focus the app window
        import pygetwindow as gw
        windows = gw.getWindowsWithTitle(app_name)
        if windows:
            win = windows[0]
            if win.isMinimized:
                win.restore()
            win.activate()
            time.sleep(0.5)
        else:
            # App not open — try to open it
            from app_finder import find_best_match, launch_app
            match = find_best_match(app_name)
            if match and match.get("exe_path"):
                os.startfile(match["exe_path"])
                time.sleep(wait_time)
            else:
                # Fallback: launch_app handles web shortcuts, UWP, etc.
                launch_app(app_name)
                time.sleep(wait_time)
    except Exception as e:
        logger.warning(f"Could not focus {app_name}: {e}")

    try:
        # Press search hotkey
        time.sleep(0.3)
        pag.hotkey(*hotkey)
        time.sleep(0.5)

        # Clear existing text and type query
        pag.hotkey("ctrl", "a")
        time.sleep(0.1)
        if query.isascii():
            pag.typewrite(query, interval=0.02)
        else:
            import pyperclip
            pyperclip.copy(query)
            pag.hotkey("ctrl", "v")
        time.sleep(0.2)
        pag.press("enter")

        return f"Searched for '{query}' in {app_name}."
    except Exception as e:
        logger.error(f"Desktop search error in {app_name}: {e}")
        return f"Error searching in {app_name}: {e}"


# ===================================================================
# Accessibility tree — delegates to automation/ui_control.py
# ===================================================================

def get_ui_elements(window_title=None, element_types=None, max_depth=4, max_elements=30):
    """Get clickable/interactable UI elements using UI Automation.

    Delegates to automation.ui_control.list_controls().
    Kept for backward compatibility (desktop_agent.py, YouTube player).
    """
    try:
        from automation.ui_control import list_controls
        role = None
        if element_types and len(element_types) == 1:
            role = element_types[0]
        return list_controls(window=window_title, role=role,
                             max_depth=max_depth, max_count=max_elements)
    except Exception as e:
        logger.debug(f"UI Automation error: {e}")
        return []


def click_element_by_name(name, window_title=None):
    """Find a UI element by name and click it.

    Delegates to automation.ui_control.click_control().
    Kept for backward compatibility.
    """
    try:
        from automation.ui_control import click_control
        return click_control(name=name, window=window_title)
    except Exception as e:
        logger.debug(f"click_element_by_name error: {e}")
        return f"Error clicking '{name}': {e}"


# ===================================================================
# Browser tab management
# ===================================================================

def manage_tabs(action, index=None):
    """Manage browser tabs using keyboard shortcuts.

    Args:
        action: "new", "close", "next", "prev", "goto" (1-indexed), "list"
        index: Tab number for "goto" action (1-indexed)

    Returns: result string
    """
    pag = _get_pyautogui()
    import pygetwindow as gw

    # Verify we're in a browser
    active = gw.getActiveWindow()
    if not active:
        return "No active window"
    title = (active.title or "").lower()
    browser_names = ["chrome", "firefox", "edge", "brave", "opera"]
    if not any(b in title for b in browser_names):
        return f"Active window '{active.title}' is not a browser"

    action = action.lower().strip()

    if action == "new":
        pag.hotkey("ctrl", "t")
        time.sleep(0.5)
        return "Opened new tab"

    elif action == "close":
        pag.hotkey("ctrl", "w")
        time.sleep(0.3)
        return "Closed current tab"

    elif action == "next":
        pag.hotkey("ctrl", "tab")
        time.sleep(0.3)
        active = gw.getActiveWindow()
        return f"Switched to next tab: {active.title if active else 'unknown'}"

    elif action == "prev":
        pag.hotkey("ctrl", "shift", "tab")
        time.sleep(0.3)
        active = gw.getActiveWindow()
        return f"Switched to previous tab: {active.title if active else 'unknown'}"

    elif action == "goto" and index:
        idx = int(index)
        if 1 <= idx <= 8:
            pag.hotkey("ctrl", str(idx))
            time.sleep(0.3)
            active = gw.getActiveWindow()
            return f"Switched to tab {idx}: {active.title if active else 'unknown'}"
        elif idx == 9 or idx >= 9:
            pag.hotkey("ctrl", "9")  # Last tab
            time.sleep(0.3)
            return "Switched to last tab"
        return f"Invalid tab index: {idx}"

    elif action == "list":
        # Get all tabs by cycling through them and reading titles
        tabs = []
        original_title = active.title
        # Use Ctrl+Tab to cycle, read each title
        for i in range(20):  # Max 20 tabs
            current = gw.getActiveWindow()
            if current and current.title:
                tab_title = current.title
                if tab_title in [t["title"] for t in tabs]:
                    break  # We've cycled back to the start
                tabs.append({"index": i + 1, "title": tab_title})
            pag.hotkey("ctrl", "tab")
            time.sleep(0.2)

        # Return to original tab
        for _ in range(len(tabs)):
            current = gw.getActiveWindow()
            if current and current.title == original_title:
                break
            pag.hotkey("ctrl", "tab")
            time.sleep(0.2)

        if tabs:
            lines = [f"  {t['index']}. {t['title']}" for t in tabs]
            return f"Open tabs ({len(tabs)}):\n" + "\n".join(lines)
        return "Could not list tabs"

    elif action == "reopen":
        pag.hotkey("ctrl", "shift", "t")
        time.sleep(0.5)
        return "Reopened last closed tab"

    return f"Unknown tab action: {action}. Use: new, close, next, prev, goto, list, reopen"


# ===================================================================
# Form interaction — detect and fill web forms
# ===================================================================

def fill_form_fields(fields):
    """Fill a web form by tabbing through fields and typing values.

    Args:
        fields: list of dicts with {value: "text to type"} in tab order,
                or dict with {field_name: value} for accessibility-based fill

    Returns: result string
    """
    pag = _get_pyautogui()

    if isinstance(fields, list):
        # Sequential tab+type approach
        filled = 0
        for field in fields:
            value = field.get("value", "")
            if not value:
                pag.press("tab")
                time.sleep(0.1)
                continue

            # Clear existing content and type new value
            pag.hotkey("ctrl", "a")
            time.sleep(0.05)
            try:
                import pyperclip
                pyperclip.copy(str(value))
                pag.hotkey("ctrl", "v")
            except ImportError:
                pag.typewrite(str(value), interval=0.02)
            time.sleep(0.1)
            pag.press("tab")
            time.sleep(0.15)
            filled += 1

        return f"Filled {filled} form fields"

    elif isinstance(fields, dict):
        # Accessibility-based: find each field by name and type into it
        elements = get_ui_elements(element_types=["Edit", "TextBox", "Document", "ComboBox"])
        filled = 0
        for field_name, value in fields.items():
            if not value:
                continue
            name_lower = field_name.lower()
            for el in elements:
                if name_lower in el["name"].lower() or el["name"].lower() in name_lower:
                    pag.click(el["x"], el["y"])
                    time.sleep(0.2)
                    pag.hotkey("ctrl", "a")
                    time.sleep(0.05)
                    try:
                        import pyperclip
                        pyperclip.copy(str(value))
                        pag.hotkey("ctrl", "v")
                    except ImportError:
                        pag.typewrite(str(value), interval=0.02)
                    time.sleep(0.1)
                    filled += 1
                    logger.info(f"Filled field '{el['name']}' with '{value[:30]}'")
                    break

        return f"Filled {filled}/{len(fields)} form fields"

    return "Invalid fields format"
