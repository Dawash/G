"""
PopupGuardian — background agent that detects and handles screen blockers.

Runs alongside the desktop agent and proactively handles:
  - App picker dialogs ("Select an app to open...")
  - Cookie consent banners
  - Login walls / subscription prompts (pauses for user)
  - CAPTCHA challenges (pauses for user)
  - Form auto-fill (known fields)
  - System dialogs (UAC, permission, save/discard)
  - Update prompts, notifications, browser promos
  - Profile pickers

Detection uses UIA (UI Automation) for reliable, language-agnostic matching.
Falls back to title keywords and window class checks.

The guardian classifies each popup by Execution Tier:
  - Tier 0-1: Auto-dismiss (cookies, promos, default prompts, app pickers)
  - Tier 2: Auto-handle with logging (save dialogs, error dialogs)
  - Tier 3: Pause and alert user (login, CAPTCHA, payment, 2FA)
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How often to scan for popups (seconds)
_SCAN_INTERVAL = 2.0

# Popup classification
POPUP_AUTO_DISMISS = "auto_dismiss"      # Close/escape immediately
POPUP_AUTO_HANDLE = "auto_handle"        # Pick the right button/option
POPUP_HUMAN_REQUIRED = "human_required"  # Pause and notify user


class PopupGuardian:
    """Background popup monitor and handler.

    Usage:
        guardian = PopupGuardian(speak_fn=speak, goal="open notepad")
        guardian.start()   # Start background scanning
        ... do agent work ...
        guardian.stop()    # Stop scanning

    The guardian runs in a daemon thread and auto-dismisses popups
    that block the agent's goal. For Tier 3 popups (login, CAPTCHA),
    it pauses and optionally speaks an alert.
    """

    def __init__(self, speak_fn=None, goal="", on_popup=None):
        """
        Args:
            speak_fn: Optional callable(text) to speak alerts.
            goal: Current agent goal (used for smart popup handling).
            on_popup: Optional callback(popup_type, title, action_taken) for logging.
        """
        self.speak_fn = speak_fn
        self.goal = goal.lower() if goal else ""
        self.on_popup = on_popup
        self._running = False
        self._thread = None
        self._dismissed = []  # Track what we've already handled
        self._paused_for_human = False
        self._lock = threading.Lock()

    def start(self):
        """Start the background popup scanner."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True, name="PopupGuardian")
        self._thread.start()
        logger.info("PopupGuardian started")

    def stop(self):
        """Stop the background scanner."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        logger.info(f"PopupGuardian stopped (handled {len(self._dismissed)} popups)")

    @property
    def is_paused(self):
        """True if waiting for human to handle a Tier 3 popup."""
        return self._paused_for_human

    def resume(self):
        """Resume after human handled the Tier 3 popup."""
        self._paused_for_human = False

    def get_dismissed(self):
        """Get list of dismissed popup descriptions."""
        with self._lock:
            return list(self._dismissed)

    # ------------------------------------------------------------------
    # Background scan loop
    # ------------------------------------------------------------------

    def _scan_loop(self):
        """Continuously scan for popups until stopped."""
        while self._running:
            try:
                if not self._paused_for_human:
                    self._check_and_handle()
            except Exception as e:
                logger.debug(f"PopupGuardian scan error: {e}")
            time.sleep(_SCAN_INTERVAL)

    def _check_and_handle(self):
        """Single scan: detect popup and handle it."""
        # Also check for in-browser ads (YouTube, Spotify, etc.)
        self._skip_browser_ads()

        popup = self._detect_popup()
        if not popup:
            return

        popup_type, title, classification = popup

        # Skip if we already handled this exact popup
        popup_key = f"{popup_type}:{title}"
        with self._lock:
            if popup_key in self._dismissed:
                return

        logger.info(f"PopupGuardian detected: [{classification}] {popup_type} — '{title}'")

        if classification == POPUP_HUMAN_REQUIRED:
            self._handle_human_required(popup_type, title)
        elif classification == POPUP_AUTO_HANDLE:
            self._handle_auto(popup_type, title)
        else:
            self._handle_dismiss(popup_type, title)

        with self._lock:
            self._dismissed.append(popup_key)

        if self.on_popup:
            try:
                self.on_popup(popup_type, title, classification)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _detect_popup(self) -> Optional[tuple]:
        """Detect if there's a popup blocking the screen.

        Returns (popup_type, title, classification) or None.
        """
        try:
            import pygetwindow as gw
        except ImportError:
            return None

        try:
            active = gw.getActiveWindow()
            if not active or not active.title:
                return None
        except Exception:
            return None

        title = active.title
        title_lower = title.lower()

        # Skip if active window IS the goal (not a popup)
        if self.goal and self.goal in title_lower:
            return None

        # --- Check window class (fastest) ---
        win_class = self._get_window_class(active)

        # --- Classify by title keywords ---
        # Tier 3: Human required
        if any(kw in title_lower for kw in [
            "captcha", "recaptcha", "verify you are human",
            "i'm not a robot", "hcaptcha", "challenge",
        ]):
            return ("captcha", title, POPUP_HUMAN_REQUIRED)

        if any(kw in title_lower for kw in [
            "sign in", "log in", "login", "authenticate",
            "two-factor", "2fa", "verification code",
        ]) and not any(w in self.goal for w in ["sign in", "log in", "login"]):
            return ("login", title, POPUP_HUMAN_REQUIRED)

        if any(kw in title_lower for kw in [
            "payment", "checkout", "billing", "credit card",
            "purchase", "buy now",
        ]):
            return ("payment", title, POPUP_HUMAN_REQUIRED)

        if any(kw in title_lower for kw in [
            "user account control", "administrator",
        ]) or win_class == "#32770" and "admin" in title_lower:
            return ("uac", title, POPUP_HUMAN_REQUIRED)

        # Tier 1: Auto-handle (pick right option)
        if any(kw in title_lower for kw in [
            "select an app", "open with", "how do you want to open",
            "choose an app", "choose a default",
        ]):
            return ("app_picker", title, POPUP_AUTO_HANDLE)

        if any(kw in title_lower for kw in [
            "cookie", "consent", "accept all", "gdpr", "privacy",
        ]):
            return ("cookie", title, POPUP_AUTO_DISMISS)

        if any(kw in title_lower for kw in [
            "save changes", "do you want to save", "unsaved changes",
        ]):
            return ("save_dialog", title, POPUP_AUTO_HANDLE)

        if any(kw in title_lower for kw in [
            "subscription", "upgrade now", "try free", "trial",
            "get started", "premium", "buy now",
        ]) and "payment" not in title_lower:
            return ("subscription", title, POPUP_AUTO_DISMISS)

        if any(kw in title_lower for kw in [
            "default browser", "set as default", "make default",
        ]):
            return ("default_prompt", title, POPUP_AUTO_DISMISS)

        if any(kw in title_lower for kw in [
            "update available", "restart to update", "new version",
        ]):
            return ("update", title, POPUP_AUTO_DISMISS)

        if any(kw in title_lower for kw in [
            "wants to send notifications", "show notifications",
            "allow notifications", "grant permission",
        ]):
            return ("notification_permission", title, POPUP_AUTO_DISMISS)

        if any(kw in title_lower for kw in [
            "choose profile", "select profile", "profile picker",
        ]):
            return ("profile_picker", title, POPUP_AUTO_HANDLE)

        # --- Structural detection: small window with dialog buttons ---
        try:
            w, h = active.width, active.height
            if 50 < w < 900 and 50 < h < 700:
                if win_class == "#32770":
                    return ("system_dialog", title, POPUP_AUTO_HANDLE)
        except Exception:
            pass

        return None

    def _get_window_class(self, window) -> str:
        """Get Win32 window class name."""
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(window._hWnd, buf, 256)
            return buf.value
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_dismiss(self, popup_type, title):
        """Auto-dismiss: Escape → close button → Alt+F4."""
        import pyautogui
        logger.info(f"PopupGuardian auto-dismissing: {popup_type} '{title}'")

        # Try Escape first (works for most dialogs)
        pyautogui.press("escape")
        time.sleep(0.5)

        # Check if it's still there
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if active and active.title.lower() == title.lower():
                # Try clicking close/dismiss/reject button via UIA
                if not self._click_button(title, [
                    "close", "dismiss", "reject", "no thanks", "not now",
                    "later", "skip", "got it", "decline", "no",
                    "deny", "block", "maybe later",
                ]):
                    # Last resort: Alt+F4
                    pyautogui.hotkey("alt", "F4")
        except Exception:
            pass

    def _handle_auto(self, popup_type, title):
        """Auto-handle: pick the right option based on popup type."""
        logger.info(f"PopupGuardian auto-handling: {popup_type} '{title}'")

        if popup_type == "app_picker":
            self._handle_app_picker(title)
        elif popup_type == "save_dialog":
            # Don't save — dismiss to unblock agent
            self._click_button(title, [
                "don't save", "no", "discard", "cancel",
            ])
        elif popup_type == "profile_picker":
            # Click first profile or "Continue" button
            self._click_button(title, [
                "continue", "default", "ok",
            ])
        elif popup_type == "system_dialog":
            # Try OK or Cancel
            self._click_button(title, [
                "ok", "yes", "continue", "close", "cancel",
            ])

    def _handle_app_picker(self, title):
        """Handle 'Select an app to open' dialog.

        Reads the items in the picker, selects the best one based on
        file type and user preferences, and clicks 'Just once'.
        """
        try:
            from automation.ui_control import list_controls
            controls = list_controls(window=title, max_depth=4, max_count=60)
            if not controls:
                import pyautogui
                pyautogui.press("escape")
                return

            # Extract what's being opened from the title
            target = title.lower()
            for prefix in ["select an app to open", "how do you want to open",
                           "open with", "choose an app to open"]:
                target = target.replace(prefix, "")
            target = target.strip(" '\".")

            # Map file types/protocols to preferred apps
            _preferences = {
                ".html": ["firefox", "chrome", "edge", "brave"],
                ".htm": ["firefox", "chrome", "edge", "brave"],
                "http": ["firefox", "chrome", "edge", "brave"],
                ".txt": ["notepad", "visual studio code"],
                ".py": ["visual studio code", "cursor", "notepad"],
                ".pdf": ["firefox", "chrome", "adobe", "edge"],
                ".md": ["visual studio code", "cursor", "notepad"],
                "notepad": ["notepad"],
                ".csv": ["excel", "notepad"],
                ".mp3": ["windows media player"],
                ".mp4": ["vlc", "windows media player"],
            }

            # Find which preference list to use
            preferred = ["notepad", "firefox", "chrome"]  # Default
            for key, apps in _preferences.items():
                if key in target:
                    preferred = apps
                    break

            # Find clickable items in the list
            items = []
            for c in controls:
                ctype = (c.get("control_type") or "").lower()
                cname = (c.get("name") or "").strip()
                if cname and ctype in ("listitem", "button", "text", "radiobutton"):
                    items.append((cname, c.get("x", 0), c.get("y", 0)))

            # Pick the best match
            import pyautogui
            selected = False
            for pref in preferred:
                for item_name, x, y in items:
                    if pref in item_name.lower() and x > 0 and y > 0:
                        pyautogui.click(x, y)
                        time.sleep(0.3)
                        selected = True
                        logger.info(f"PopupGuardian: selected '{item_name}' in app picker")
                        break
                if selected:
                    break

            if not selected and items:
                # Click first item as fallback
                name, x, y = items[0]
                if x > 0 and y > 0:
                    pyautogui.click(x, y)
                    time.sleep(0.3)
                    logger.info(f"PopupGuardian: fallback selected '{name}'")

            # Click "Just once" or "Always" button
            time.sleep(0.3)
            self._click_button(title, ["just once", "ok", "open", "always"])

        except Exception as e:
            logger.warning(f"PopupGuardian app_picker failed: {e}")
            import pyautogui
            pyautogui.press("escape")

    def _handle_human_required(self, popup_type, title):
        """Tier 3: Pause and alert the user."""
        self._paused_for_human = True
        msg = {
            "captcha": f"I found a CAPTCHA on '{title}'. Please solve it and say 'continue'.",
            "login": f"A login screen appeared: '{title}'. Please sign in and say 'continue'.",
            "payment": f"A payment page appeared: '{title}'. Please handle it and say 'continue'.",
            "uac": f"A permission dialog appeared. Please approve it and say 'continue'.",
        }.get(popup_type, f"I need your help with: '{title}'. Say 'continue' when done.")

        logger.warning(f"PopupGuardian PAUSED: {popup_type} — {title}")
        if self.speak_fn:
            try:
                self.speak_fn(msg)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # In-browser ad skipping (YouTube, Spotify Web, etc.)
    # ------------------------------------------------------------------

    def _skip_browser_ads(self):
        """Skip in-browser video ads via CDP JavaScript injection.

        Detects YouTube pre-roll/mid-roll ads and:
        1. Clicks "Skip Ad" / "Skip Ads" button if available
        2. If no skip button, fast-forwards the ad to 0s remaining
        3. Also dismisses overlay ads and banner ads
        """
        try:
            from automation.browser_driver import _check_cdp, _get_active_tab_ws, _send_cdp_command
        except ImportError:
            return

        if not _check_cdp():
            return

        ws = _get_active_tab_ws(url_contains="youtube.com")
        if not ws:
            return

        js_skip_ads = """
        (() => {
            const results = [];

            // --- YouTube Ad Skipping ---

            // Strategy 1: Click "Skip Ad" / "Skip Ads" button
            const skipBtns = document.querySelectorAll(
                '.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, ' +
                'button.ytp-ad-skip-button, button.ytp-ad-skip-button-modern, ' +
                '[class*="skip-button"], .videoAdUiSkipButton'
            );
            for (const btn of skipBtns) {
                if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                    btn.click();
                    results.push('clicked skip button');
                }
            }

            // Also try text-based skip detection
            const allBtns = document.querySelectorAll('button, [role="button"]');
            for (const btn of allBtns) {
                const text = (btn.textContent || '').trim().toLowerCase();
                if ((text.includes('skip ad') || text.includes('skip ads') ||
                     text === 'skip') && btn.offsetWidth > 0) {
                    btn.click();
                    results.push('clicked skip via text: ' + text);
                }
            }

            // Strategy 2: If ad is playing but no skip button, fast-forward it
            const adOverlay = document.querySelector('.ytp-ad-player-overlay, .ad-showing');
            const video = document.querySelector('video');
            if (adOverlay && video && video.duration && video.duration < 120) {
                // It's a short ad — jump to end
                if (video.currentTime < video.duration - 0.5) {
                    video.currentTime = video.duration;
                    results.push('fast-forwarded ad');
                }
            }

            // Check if ad is playing via class on player
            const player = document.querySelector('.html5-video-player');
            if (player && player.classList.contains('ad-showing') && video) {
                if (video.duration && video.duration < 120 && video.currentTime < video.duration - 0.5) {
                    video.currentTime = video.duration;
                    results.push('skipped ad via player class');
                }
            }

            // Strategy 3: Close overlay/banner ads
            const overlayClose = document.querySelectorAll(
                '.ytp-ad-overlay-close-button, .ytp-ad-overlay-close-container, ' +
                '[class*="ad-overlay-close"], .ytp-ad-text-overlay .ytp-ad-overlay-close-button'
            );
            for (const btn of overlayClose) {
                if (btn.offsetWidth > 0) {
                    btn.click();
                    results.push('closed overlay ad');
                }
            }

            // Strategy 4: Mute ad if still playing (less intrusive)
            // Only mute if ad, not if user's video
            if (player && player.classList.contains('ad-showing') && video && !video.muted) {
                // Don't mute — user might not want that. Just skip.
            }

            return results.length > 0 ? results.join(', ') : '';
        })()
        """

        try:
            result = _send_cdp_command(ws, "Runtime.evaluate", {
                "expression": js_skip_ads,
                "returnByValue": True,
            })
            if result:
                value = str(result.get("result", {}).get("value", ""))
                if value:
                    logger.info(f"PopupGuardian ad-skip: {value}")
                    with self._lock:
                        self._dismissed.append(f"browser_ad:{value[:60]}")
        except Exception as e:
            logger.debug(f"PopupGuardian ad-skip error: {e}")

    def _click_button(self, window_title, button_names):
        """Try to click a button by name using UIA. Returns True if clicked."""
        try:
            from automation.ui_control import list_controls
            import pyautogui

            controls = list_controls(window=window_title, max_depth=3, max_count=40)
            if not controls:
                return False

            for c in controls:
                ctype = (c.get("control_type") or "").lower()
                cname = (c.get("name") or "").lower().strip()
                if ctype == "button" and cname:
                    for target in button_names:
                        if target in cname:
                            x, y = c.get("x", 0), c.get("y", 0)
                            if x > 0 and y > 0:
                                pyautogui.click(x, y)
                                logger.info(f"PopupGuardian: clicked '{c['name']}'")
                                return True
            return False
        except Exception as e:
            logger.debug(f"PopupGuardian click_button failed: {e}")
            return False


# ------------------------------------------------------------------
# Convenience: one-shot popup check (no background thread)
# ------------------------------------------------------------------

def check_and_handle_popup(goal="", speak_fn=None) -> Optional[str]:
    """One-shot popup check. Returns popup description if handled, None otherwise.

    Use this in the agent loop instead of running the full background guardian.
    """
    guardian = PopupGuardian(speak_fn=speak_fn, goal=goal)
    popup = guardian._detect_popup()
    if not popup:
        return None

    popup_type, title, classification = popup
    logger.info(f"Popup detected: [{classification}] {popup_type} — '{title}'")

    if classification == POPUP_HUMAN_REQUIRED:
        guardian._handle_human_required(popup_type, title)
        return f"PAUSED: {popup_type} — {title}"
    elif classification == POPUP_AUTO_HANDLE:
        guardian._handle_auto(popup_type, title)
        return f"Handled: {popup_type} — {title}"
    else:
        guardian._handle_dismiss(popup_type, title)
        return f"Dismissed: {popup_type} — {title}"
