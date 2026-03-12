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
        q_base = re.sub(r'\s*(music|songs?|playlist|mix)\s*$', '', q_lower).strip()
        is_vague = q_lower in _VAGUE_MUSIC or q_base in _GENRE_WORDS or len(q_base) <= 3
        if is_vague and quick_chat_fn:
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
                        logger.info(f"Music genre '{query}' -> specific song: '{suggestion}'")
                        query = suggestion
            except Exception:
                pass
            if query.lower().strip() == q_lower:
                query = "Today's Top Hits" if q_lower in _VAGUE_MUSIC else f"best {q_base} songs"

    if action in ("play", "play_query") and query:
        if app == "spotify":
            if _open_spotify_app():
                try:
                    time.sleep(1.5)

                    # Step 1: Search via Spotify URI protocol (direct API — most reliable)
                    try:
                        from urllib.parse import quote
                        import subprocess as _sp
                        search_uri = f"spotify:search:{quote(query)}"
                        _sp.Popen(["cmd", "/c", "start", "", search_uri],
                                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                        time.sleep(3)
                    except Exception:
                        # Fallback: Ctrl+K keyboard search
                        try:
                            from automation.ui_control import focus_window, set_control_text
                            focus_window("Spotify")
                        except Exception:
                            import pygetwindow as gw
                            wins = gw.getWindowsWithTitle("Spotify")
                            if wins:
                                try:
                                    wins[0].activate()
                                except Exception:
                                    pass
                        time.sleep(0.3)
                        import pyautogui
                        pyautogui.FAILSAFE = False
                        pyautogui.hotkey("ctrl", "k")
                        time.sleep(0.5)
                        pyautogui.hotkey("ctrl", "a")
                        time.sleep(0.1)
                        try:
                            import pyperclip
                            pyperclip.copy(query)
                            pyautogui.hotkey("ctrl", "v")
                        except ImportError:
                            pyautogui.typewrite(query, interval=0.03)
                        time.sleep(3.5)

                    # Step 2: Check for no results before trying to click
                    from computer import _click_first_spotify_song, _spotify_no_results
                    if _spotify_no_results():
                        logger.info(f"Spotify: no results for '{query}'")
                        return f"No results found for '{query}' on Spotify. Try a different search term."

                    # Step 3: Click first result via UIA (proper desktop automation)
                    if _click_first_spotify_song():
                        # Verify playback
                        try:
                            from tools.outcome import check_spotify_playing
                            playing, title, evidence = check_spotify_playing(timeout=4)
                            if playing:
                                logger.info(f"Spotify verified playing: {title}")
                                return f"Playing '{query}' on Spotify."
                            # Retry with media key
                            _press_media_key(VK_MEDIA_PLAY_PAUSE)
                            time.sleep(2)
                            playing2, title2, _ = check_spotify_playing(timeout=3)
                            if playing2:
                                return f"Playing '{query}' on Spotify."
                        except ImportError:
                            pass
                        return f"Playing '{query}' on Spotify."
                    # Check again — click failure might be due to no results
                    if _spotify_no_results():
                        return f"No results found for '{query}' on Spotify. Try a different search term."
                    logger.warning("Spotify UIA click-to-play failed")
                except Exception as e:
                    logger.error(f"Failed to search in Spotify: {e}")
            return f"Opened Spotify and searched for '{query}', but couldn't auto-play. Click a result to play."

        elif app == "youtube":
            from urllib.parse import quote_plus
            url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

            # Use CDP-enabled browser for proper web automation
            _opened = False
            try:
                from automation.browser_driver import browser_navigate, is_cdp_available
                if not is_cdp_available():
                    from automation.cdp_session import CDPSession
                    CDPSession().ensure_chrome()
                    time.sleep(2)
                if is_cdp_available():
                    browser_navigate(url)
                    _opened = True
            except Exception:
                pass
            if not _opened:
                import webbrowser
                webbrowser.open(url)

            time.sleep(5)  # Wait for search results to load
            from computer import _click_first_youtube_video
            if _click_first_youtube_video():
                return f"Playing '{query}' on YouTube."
            # Retry after additional wait
            logger.info("YouTube click retry after additional wait")
            time.sleep(3)
            if _click_first_youtube_video():
                return f"Playing '{query}' on YouTube."
            return f"Searched for '{query}' on YouTube but couldn't auto-play. Click a result to play."

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
        return "Volume increased."

    elif action in ("volume_down", "quieter"):
        for _ in range(5):
            _press_media_key(VK_VOLUME_DOWN)
        return "Volume decreased."

    elif action == "mute":
        _press_media_key(VK_VOLUME_MUTE)
        return "Volume muted."

    else:
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
        return "Toggled music playback."
