"""
Media control: Spotify, system media keys, volume.

Extracted from: brain.py _play_music()
Original sources: brain_defs.py _press_media_key(), VK_MEDIA_* constants,
                  _open_spotify_app(), _wait_for_process()

Responsibility:
  - Play/pause/next/previous via Windows media keys
  - Volume up/down/mute
  - Spotify app launch and search-based playback
  - YouTube fallback
  - Smart genre-to-song expansion via LLM
"""

import re
import time
import logging

from brain_defs import (
    _press_media_key, VK_MEDIA_PLAY_PAUSE, VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PREV_TRACK, VK_VOLUME_UP, VK_VOLUME_DOWN, VK_VOLUME_MUTE,
    _wait_for_process, _open_spotify_app,
)

logger = logging.getLogger(__name__)

# Words that indicate vague/generic music requests
_VAGUE_MUSIC = {
    "good music", "nice music", "something good", "something nice",
    "some music", "something", "anything", "whatever", "random",
    "good songs", "nice songs", "good song", "nice song",
    "good", "nice", "best", "great", "cool",
    "vibe", "vibes", "music", "a song", "song",
    # Natural speech variants often from STT
    "some good music", "some good songs", "some good song",
    "some nice music", "some nice songs", "a good song",
    "some cool music", "some great music", "something cool",
    "something relaxing", "something upbeat", "something chill",
    "something to listen to", "some tunes", "some jams",
}

# Genre keywords that need expansion to specific songs
_GENRE_WORDS = {
    "romantic", "chill", "sad", "happy", "party", "dance", "rock",
    "pop", "jazz", "classical", "hip hop", "rap", "country", "r&b",
    "rnb", "edm", "lofi", "lo-fi", "indie", "metal", "blues",
    "folk", "soul", "funk", "reggae", "latin", "bollywood",
    "workout", "study", "sleep", "relax", "focus", "energetic",
    "upbeat", "mellow", "acoustic", "love", "heartbreak",
}


def play_music(action, query=None, app="spotify", last_user_input="", quick_chat_fn=None):
    """Control music playback via Spotify URI or media keys.

    Args:
        action: play, play_query, pause, next, previous, volume_up/down, mute
        query: Song/artist name or genre.
        app: Target app (spotify/youtube).
        last_user_input: Raw user input for query extraction fallback.
        quick_chat_fn: Optional LLM function for genre->song expansion.

    Returns:
        str: Result message.
    """
    action = action.lower().strip()
    app = (app or "spotify").lower().strip()

    # Clean query: strip trailing platform names
    if query:
        query = re.sub(r'\s+(on|in|from|using|via|with)\s+(spotify|youtube|music player)$',
                       '', query, flags=re.IGNORECASE).strip()

    # Extract query from user input if missing
    if action in ("play", "play_query") and not query and last_user_input:
        for pat in [r"play (.+?) on (?:spotify|youtube)",
                    r"play (.+?) (?:on|in|from|using)",
                    r"play (.+)"]:
            m = re.search(pat, last_user_input, re.IGNORECASE)
            if m:
                extracted = m.group(1).strip()
                extracted = re.sub(r'^(a |an |some |the |any |or |me )', '', extracted).strip()
                extracted = re.sub(r'\s*(on|in|from|using|via|with|through)\s*(spotify|youtube|music|app).*$',
                                   '', extracted, flags=re.IGNORECASE).strip()
                _SKIP = {"music", "song", "songs", "video", "something", "or", "and", "it", ""}
                if extracted and extracted.lower() not in _SKIP:
                    query = extracted
                    break

    # Expand vague/genre queries into specific songs
    if action in ("play", "play_query") and query:
        q_lower = query.lower().strip()
        # Strip leading filler words: "some", "a", "any", "me", "me some", "me a"
        q_cleaned = re.sub(r'^(?:some|a|any|me\s+(?:some|a)?)\s+', '', q_lower).strip()
        q_base = re.sub(r'\s*(music|songs?|playlist|mix|tracks?)\s*$', '', q_cleaned).strip()
        is_vague = (q_lower in _VAGUE_MUSIC or q_cleaned in _VAGUE_MUSIC
                    or q_base in _VAGUE_MUSIC or q_base in _GENRE_WORDS
                    or len(q_base) <= 3)
        if is_vague:
            # Try LLM expansion first (ask for a specific song)
            if quick_chat_fn:
                try:
                    suggestion = quick_chat_fn(
                        f"Suggest ONE specific popular song (song name and artist) for: '{query}'. "
                        f"Reply with ONLY the song name and artist, nothing else. Example: 'Perfect by Ed Sheeran'"
                    )
                    if suggestion and len(suggestion.strip()) > 3:
                        suggestion = suggestion.strip().strip('"').strip("'")
                        suggestion = re.sub(r'^(here\'s one:|how about|i suggest|try)\s*', '',
                                            suggestion, flags=re.I).strip()
                        suggestion = re.sub(r'^[\"\']|[\"\']$', '', suggestion).strip()
                        if len(suggestion) > 3:
                            logger.info(f"Music vague '{query}' -> specific song: '{suggestion}'")
                            query = suggestion
                except Exception:
                    pass
            # Fallback: if LLM didn't expand (or wasn't available), use a safe default
            if query.lower().strip() == q_lower:
                _is_vague_word = (q_lower in _VAGUE_MUSIC or q_cleaned in _VAGUE_MUSIC
                                  or q_base in _VAGUE_MUSIC)
                if _is_vague_word:
                    query = "Today's Top Hits"
                elif q_base in _GENRE_WORDS:
                    query = f"best {q_base} songs"
                else:
                    query = f"top {q_base} playlist"
                logger.info(f"Music vague fallback: '{q_lower}' -> '{query}'")

    if action in ("play", "play_query") and query:
        # Start PopupGuardian to auto-dismiss popups during media playback
        _guardian = None
        try:
            from agents.popup_guardian import PopupGuardian
            _guardian = PopupGuardian(goal=f"play {query} on {app}")
            _guardian.start()
        except Exception:
            pass

        if app == "spotify":
            try:
                # Check failure journal: if desktop failed 3+ times, skip to web player
                _skip_desktop = False
                try:
                    from core.failure_journal import get_default_journal
                    fj = get_default_journal()
                    if fj:
                        stats = fj.get_failure_stats()
                        desktop_fails = stats.get("by_route", {}).get("spotify_desktop", 0)
                        if desktop_fails >= 3:
                            logger.info(f"Spotify: skipping desktop (failed {desktop_fails}x), using web player")
                            _skip_desktop = True
                except Exception:
                    pass

                # Strategy 1: Spotify Desktop App (URI protocol + keyboard)
                if not _skip_desktop:
                    desktop_result = _play_spotify_desktop(query)
                    if desktop_result:
                        return desktop_result

                # Strategy 2: Spotify Web Player via CDP (reliable, portable)
                web_result = _play_spotify_web(query)
                if web_result:
                    return web_result
            finally:
                if _guardian:
                    _guardian.stop()

            return f"I found results for '{query}' on Spotify, but couldn't start playback automatically."

        elif app == "youtube":
            try:
                from urllib.parse import quote_plus
                url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

                # Use CDP-enabled browser for proper web automation
                _opened = False
                try:
                    from automation.browser_driver import browser_navigate, is_cdp_available, _check_cdp
                    if not is_cdp_available():
                        from automation.cdp_session import CDPSession
                        CDPSession().ensure_chrome()
                        time.sleep(2)
                    if _check_cdp(force=True) or is_cdp_available():
                        browser_navigate(url)
                        _opened = True
                except Exception:
                    pass
                if not _opened:
                    import webbrowser
                    webbrowser.open(url)

                # Poll for YouTube page readiness (up to 8s) instead of fixed sleep
                for _yt_wait in range(8):
                    time.sleep(1)
                    try:
                        _yt_ws = _get_active_tab_ws(url_contains="youtube.com")
                        if _yt_ws:
                            _yt_check = _send_cdp_command(_yt_ws, "Runtime.evaluate", {
                                "expression": "document.readyState",
                                "returnByValue": True
                            })
                            if _yt_check and "complete" in str(_yt_check.get("result", {}).get("value", "")):
                                break
                    except Exception:
                        pass
                from computer import _click_first_youtube_video, _skip_youtube_ads
                if _click_first_youtube_video():
                    # Auto-skip ads after video starts
                    _skip_youtube_ads()
                    return f"Playing '{query}' on YouTube."
            finally:
                if _guardian:
                    _guardian.stop()
            return f"I found results for '{query}' on YouTube, but couldn't start playback automatically."

    elif action == "play":
        if app == "spotify":
            if not _open_spotify_app():
                return "Could not open Spotify. Is it installed on this computer?"
            time.sleep(1)
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        return "Playing music."

    elif action == "pause":
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        return "Music paused."

    elif action == "next":
        _press_media_key(VK_MEDIA_NEXT_TRACK)
        return "Skipped to next track."

    elif action == "previous":
        _press_media_key(VK_MEDIA_PREV_TRACK)
        return "Went to previous track."

    elif action in ("volume_up", "louder"):
        for _ in range(5):
            _press_media_key(VK_VOLUME_UP)
        return "Turned the volume up."

    elif action in ("volume_down", "quieter"):
        for _ in range(5):
            _press_media_key(VK_VOLUME_DOWN)
        return "Turned the volume down."

    elif action in ("mute", "unmute"):
        _press_media_key(VK_VOLUME_MUTE)
        return "Volume unmuted." if action == "unmute" else "Volume muted."

    else:
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        return "Toggled music playback."


# ===================================================================
# Spotify Strategies — Desktop App & Web Player
# ===================================================================

def _play_spotify_desktop(query):
    """Try to play music via Spotify desktop app.

    Uses URI protocol to search, then keyboard navigation to play.
    Fast-fail: max ~15 seconds. Falls through to web player if this fails.
    Returns result string on success, None on failure.
    """
    # Use context: skip open if already running (saves ~2 seconds)
    _already_running = False
    try:
        import subprocess as _sp
        proc = _sp.run(["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV"],
                       capture_output=True, text=True, timeout=5)
        _already_running = "spotify.exe" in proc.stdout.lower()
    except Exception:
        pass

    if not _already_running:
        if not _open_spotify_app():
            return None
        time.sleep(1.5)
    try:
        time.sleep(0.5 if _already_running else 1)

        # Search via Spotify URI protocol
        try:
            from urllib.parse import quote
            import subprocess as _sp
            search_uri = f"spotify:search:{quote(query)}"
            _sp.Popen(["cmd", "/c", "start", "", search_uri],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            time.sleep(3)
        except Exception:
            return None

        # Quick keyboard attempts only (no vision, no position clicks)
        from computer import _force_focus_spotify, _check_spotify_playing
        import pyautogui
        pyautogui.FAILSAFE = False

        _force_focus_spotify()
        time.sleep(0.5)

        # Try Tab→Enter (moves from search bar to first result)
        pyautogui.press("tab")
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(3)
        if _check_spotify_playing():
            return f"Playing '{query}' on Spotify."

        # Try Down→Enter
        _force_focus_spotify()
        time.sleep(0.3)
        pyautogui.press("down")
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(3)
        if _check_spotify_playing():
            return f"Playing '{query}' on Spotify."

        # Try media Play key (if something was already queued)
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        time.sleep(2)
        if _check_spotify_playing():
            return f"Playing '{query}' on Spotify."

    except Exception as e:
        logger.debug(f"Spotify desktop failed: {e}")

    # Record failure in failure journal for future strategy decisions
    try:
        from core.failure_journal import record_failure
        record_failure(
            goal=f"play {query} on spotify desktop",
            route="spotify_desktop",
            error_class="app_layout_drift",
            tool_sequence=["spotify_uri_search", "keyboard_navigation"],
            error_text="Desktop keyboard navigation failed to start playback",
        )
    except Exception:
        pass
    return None


def _play_spotify_web(query):
    """Play music via Spotify Web Player (open.spotify.com) using CDP.

    Reliable, portable approach — same pipeline as YouTube.
    Uses JavaScript DOM manipulation via Chrome DevTools Protocol.
    Returns result string on success, None on failure.
    """
    try:
        from urllib.parse import quote_plus
        from automation.browser_driver import (
            browser_navigate, is_cdp_available, _check_cdp,
            _get_active_tab_ws, _send_cdp_command, browser_get_url,
        )
    except ImportError:
        logger.debug("Spotify Web: browser_driver not available")
        return None

    # Ensure CDP is available (force=True after launch to bypass stale cache)
    if not _check_cdp():
        try:
            from automation.cdp_session import CDPSession
            CDPSession().ensure_chrome()
            time.sleep(2)
        except Exception:
            pass
    if not _check_cdp(force=True):
        logger.debug("Spotify Web: CDP not available")
        return None

    # Navigate to Spotify Web search
    search_url = f"https://open.spotify.com/search/{quote_plus(query)}"
    logger.info(f"Spotify Web: navigating to {search_url}")
    browser_navigate(search_url)

    # Smart wait: poll for page readiness instead of fixed sleep
    _page_ready = False
    for _wait in range(8):  # Up to 8s
        time.sleep(1)
        ws = _get_active_tab_ws(url_contains="spotify.com")
        if not ws:
            continue
        # Check if Spotify loaded or if we're on a login page
        _check = _send_cdp_command(ws, "Runtime.evaluate", {
            "expression": "document.readyState + '|' + document.title + '|' + (document.querySelector('[data-testid=\"login-button\"], [data-testid=\"login-form\"], form[action*=\"login\"]') ? 'LOGIN' : 'OK')",
            "returnByValue": True
        })
        if _check:
            _val = str(_check.get("result", {}).get("value", ""))
            if "LOGIN" in _val:
                logger.warning("Spotify Web: login required — user not logged in to CDP Chrome profile")
                return "Spotify Web Player requires login. Please open Chrome and log into open.spotify.com first."
            if "complete" in _val.lower():
                _page_ready = True
                break

    if not _page_ready:
        ws = _get_active_tab_ws(url_contains="spotify.com")
    if not ws:
        return None

    # JavaScript: click the first play button on Spotify Web search results
    js_play = """
    (() => {
        // Strategy A: Find and click first song row's play button
        const rows = document.querySelectorAll(
            '[data-testid="tracklist-row"], [data-testid="track-row"], [role="row"]'
        );
        for (const row of rows) {
            const playBtn = row.querySelector('button[data-testid="play-button"], button[aria-label*="Play"]');
            if (playBtn) {
                playBtn.click();
                return 'clicked play button in track row';
            }
            const titleLink = row.querySelector('a[href*="/track/"]');
            if (titleLink) {
                titleLink.click();
                return 'clicked track link: ' + (titleLink.textContent || '').trim().substring(0, 40);
            }
        }

        // Strategy B: Find any visible "Play" button
        const playButtons = document.querySelectorAll(
            'button[data-testid="play-button"], button[aria-label*="Play"], button[aria-label*="play"]'
        );
        for (const btn of playButtons) {
            if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.click();
                return 'clicked play button';
            }
        }

        // Strategy C: Click first card/item in search results
        const cards = document.querySelectorAll(
            '[data-testid="card-clickable"], [data-testid="top-result-card"], [data-testid="herocard-click-handler"]'
        );
        for (const card of cards) {
            const playBtn = card.querySelector('button[data-testid="play-button"], button[aria-label*="Play"]');
            if (playBtn) {
                playBtn.click();
                return 'clicked card play button';
            }
            card.click();
            return 'clicked card: ' + (card.textContent || '').trim().substring(0, 40);
        }

        // Strategy D: Click first link with /track/ or /playlist/ or /album/
        const links = document.querySelectorAll(
            'a[href*="/track/"], a[href*="/playlist/"], a[href*="/album/"]'
        );
        for (const link of links) {
            if (link.offsetWidth > 0 && link.textContent.trim().length > 0) {
                link.click();
                return 'clicked link: ' + link.textContent.trim().substring(0, 40);
            }
        }

        // Strategy E: Double-click first visible content row (generic fallback)
        const anyRows = document.querySelectorAll('[role="listitem"], [role="gridcell"], [data-testid]');
        for (const el of anyRows) {
            if (el.offsetWidth > 30 && el.textContent.trim().length > 2) {
                el.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
                return 'double-clicked content element';
            }
        }

        return 'no playable elements found';
    })()
    """

    # Retry up to 3 times (results may still be rendering)
    for attempt in range(3):
        result = _send_cdp_command(
            ws, "Runtime.evaluate",
            {"expression": js_play, "returnByValue": True}
        )
        if result:
            value = str(result.get("result", {}).get("value", ""))
            if "clicked" in value.lower():
                logger.info(f"Spotify Web CDP (attempt {attempt+1}): {value}")
                time.sleep(2)
                return f"Playing '{query}' on Spotify Web Player."
            elif "no playable" in value:
                if attempt < 2:
                    logger.info(f"Spotify Web: no elements yet, waiting (attempt {attempt+1})")
                    time.sleep(2)
                    continue
        else:
            if attempt < 2:
                time.sleep(2)
                continue

    logger.warning("Spotify Web: all CDP strategies failed")
    return None
