"""Browser executor — typed operations with state tracking.

Every method captures state_before/state_after via BrowserObserver.
Uses tiered strategy: CDP → UIA → keyboard → default browser.
"""

import logging
import time

from automation.executors.base import ActionResult
from automation.observers.browser_observer import BrowserObserver

logger = logging.getLogger(__name__)

_observer = BrowserObserver()


class BrowserExecutor:
    """Typed browser operations with state tracking and tiered fallback."""

    def __init__(self, observer=None):
        self._obs = observer or _observer

    def _snapshot(self):
        """Quick browser state for before/after."""
        return {
            "url": self._obs.get_current_url(),
            "title": self._obs.get_current_title(),
            "tab_count": self._obs.get_tab_count(),
            "is_running": self._obs.is_browser_running(),
        }

    def _find_chrome_path(self):
        """Find Chrome executable path from registry."""
        try:
            import winreg
            for key_path in [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            ]:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                    path = winreg.QueryValue(key, None)
                    winreg.CloseKey(key)
                    if path:
                        return path
                except WindowsError:
                    continue
        except Exception:
            pass
        return None

    def _is_cdp_available(self):
        """Check if CDP port 9222 is reachable."""
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:9222/json/version",
                                  timeout=0.5)
            return True
        except Exception:
            return False

    def ensure_browser(self):
        """Ensure a browser is running with CDP enabled.

        If Chrome is running but CDP is not available, it cannot be
        restarted automatically (user would lose tabs). In that case
        we proceed without CDP — keyboard/UIA fallbacks still work.

        Returns:
            ActionResult
        """
        browser_running = self._obs.is_browser_running()
        cdp_up = self._is_cdp_available()

        # Best case: browser + CDP already available
        if browser_running and cdp_up:
            return ActionResult(ok=True, strategy_used="already_running",
                              message="Browser running with CDP.")

        # Browser running but no CDP — can't restart (would lose tabs)
        if browser_running and not cdp_up:
            logger.info("Chrome running without CDP. Using keyboard/UIA fallback. "
                       "To enable CDP, restart Chrome with --remote-debugging-port=9222")
            return ActionResult(
                ok=True, strategy_used="no_cdp_fallback",
                message="Browser running (no CDP — using keyboard fallback). "
                        "Tip: restart Chrome with --remote-debugging-port=9222 for better automation.",
            )

        # No browser at all — launch Chrome with CDP
        chrome_path = self._find_chrome_path()
        if chrome_path:
            try:
                import subprocess
                subprocess.Popen([
                    chrome_path,
                    "--remote-debugging-port=9222",
                    "--no-first-run",
                    "--no-default-browser-check",
                ])
                time.sleep(2.0)
                is_up = self._obs.is_browser_running()
                cdp_ok = self._is_cdp_available()
                return ActionResult(
                    ok=is_up, strategy_used="launch_chrome_cdp",
                    verified=is_up and cdp_ok,
                    message=f"Launched Chrome {'with' if cdp_ok else 'without'} CDP.",
                )
            except Exception as e:
                logger.debug(f"Chrome launch error: {e}")

        # Fallback: open default browser (no CDP)
        try:
            import webbrowser
            webbrowser.open("about:blank")
            time.sleep(1.5)
            return ActionResult(
                ok=True, strategy_used="webbrowser",
                message="Opened default browser (no CDP support).",
            )
        except Exception as e:
            return ActionResult(ok=False, error=str(e))

    def navigate(self, url):
        """Navigate the active tab to a URL.

        Tier 1: CDP Page.navigate
        Tier 2: UIA address bar (Ctrl+L + type)
        Tier 3: webbrowser.open (last resort)
        """
        if not url:
            return ActionResult(ok=False, error="No URL provided.")

        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url

        before = self._snapshot()

        # Tier 1: CDP
        try:
            from automation.browser_driver import browser_navigate, _check_cdp
            if _check_cdp():
                result = browser_navigate(url)
                time.sleep(0.5)
                after = self._snapshot()
                # Verify URL changed
                url_ok = url.split("//", 1)[-1].split("/")[0] in after.get("url", "")
                return ActionResult(
                    ok=True, strategy_used="cdp",
                    state_before=before, state_after=after,
                    verified=url_ok,
                    message=f"Navigated to {url}",
                )
        except Exception as e:
            logger.debug(f"CDP navigate error: {e}")

        # Tier 2: keyboard (Ctrl+L, type URL)
        if self._obs.is_browser_running():
            try:
                import pyautogui
                import pyperclip

                # Save user's clipboard before overwriting
                saved_clipboard = None
                try:
                    saved_clipboard = pyperclip.paste()
                except Exception:
                    pass

                # Focus browser first
                from automation.ui_control import find_window
                for name in ("Chrome", "Edge", "Firefox", "Brave"):
                    win = find_window(name)
                    if win:
                        win.set_focus()
                        time.sleep(0.2)
                        break

                pyautogui.hotkey("ctrl", "l")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyperclip.copy(url)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.1)
                pyautogui.press("enter")
                time.sleep(1.0)

                # Restore user's clipboard
                if saved_clipboard is not None:
                    try:
                        pyperclip.copy(saved_clipboard)
                    except Exception:
                        pass

                after = self._snapshot()
                return ActionResult(
                    ok=True, strategy_used="keyboard",
                    state_before=before, state_after=after,
                    verified=url.split("//", 1)[-1].split("/")[0] in after.get("url", ""),
                    message=f"Navigating to {url}",
                )
            except Exception as e:
                logger.debug(f"Keyboard navigate error: {e}")

        # Tier 3: webbrowser.open
        try:
            import webbrowser
            webbrowser.open(url)
            time.sleep(1.5)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="webbrowser",
                state_before=before, state_after=after,
                message=f"Opened {url} in default browser.",
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def click_element(self, selector=None, text=None):
        """Click an element on the page.

        Tier 1: CDP JS click (by selector or text search)
        Tier 2: UIA click_control
        """
        if not selector and not text:
            return ActionResult(ok=False, error="Provide selector or text.")

        before = self._snapshot()

        # Tier 1: CDP
        try:
            from automation.browser_driver import browser_click, _check_cdp
            if _check_cdp():
                result = browser_click(selector=selector, text=text)
                time.sleep(0.3)
                after = self._snapshot()
                ok = result and "not found" not in result.lower()
                return ActionResult(
                    ok=ok, strategy_used="cdp",
                    state_before=before, state_after=after,
                    verified=before != after,  # Something changed
                    message=result,
                )
        except Exception as e:
            logger.debug(f"CDP click error: {e}")

        # Tier 2: UIA
        try:
            from automation.ui_control import click_control
            target = text or selector
            result = click_control(name=target)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="uia",
                state_before=before, state_after=after,
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def fill_field(self, selector=None, field_name=None, text=""):
        """Fill a form field.

        Tier 1: CDP JS fill
        Tier 2: UIA set_control_text
        """
        if not text:
            return ActionResult(ok=False, error="No text to fill.")

        before = self._snapshot()

        # Tier 1: CDP
        try:
            from automation.browser_driver import browser_fill, _check_cdp
            if _check_cdp():
                result = browser_fill(selector=selector, text=text,
                                      field_name=field_name)
                ok = result and "not found" not in result.lower()
                return ActionResult(
                    ok=ok, strategy_used="cdp",
                    state_before=before,
                    message=result,
                )
        except Exception as e:
            logger.debug(f"CDP fill error: {e}")

        # Tier 2: UIA
        try:
            from automation.ui_control import set_control_text
            target = field_name or selector
            result = set_control_text(name=target, text=text)
            return ActionResult(
                ok=True, strategy_used="uia",
                state_before=before,
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def read_page(self, selector=None):
        """Read page text content.

        Tier 1: CDP JS extraction
        Tier 2: web_agent.web_read (via URL)
        """
        # Tier 1: CDP
        try:
            from automation.browser_driver import browser_read, _check_cdp
            if _check_cdp():
                text = browser_read(selector)
                if text and "could not read" not in text.lower():
                    return ActionResult(
                        ok=True, strategy_used="cdp",
                        state_after={"text_length": len(text)},
                        verified=True,
                        message=text,
                    )
        except Exception:
            pass

        # Tier 2: web_agent via URL
        url = self._obs.get_current_url()
        if url and url.startswith("http"):
            try:
                from web_agent import web_read
                text = web_read(url)
                if text:
                    return ActionResult(
                        ok=True, strategy_used="web_agent",
                        state_after={"text_length": len(text)},
                        message=text,
                    )
            except Exception:
                pass

        return ActionResult(ok=False, error="Could not read page content.")

    def get_page_snapshot(self):
        """Get structured page snapshot (links, inputs, buttons).

        No side effects — delegates to observer.
        """
        snap = self._obs.get_page_snapshot()
        return ActionResult(
            ok=bool(snap.url or snap.links or snap.inputs),
            strategy_used="cdp" if self._obs.is_cdp_available() else "none",
            state_after={
                "url": snap.url, "title": snap.title,
                "link_count": len(snap.links),
                "input_count": len(snap.inputs),
                "button_count": len(snap.buttons),
            },
            verified=True,
            message=f"Page: {snap.title} | {len(snap.links)} links, "
                    f"{len(snap.inputs)} inputs, {len(snap.buttons)} buttons",
        )

    def switch_tab(self, index=None, title=None):
        """Switch to a browser tab.

        Tier 1: CDP Page.bringToFront
        Tier 2: Ctrl+<number>
        """
        before = self._snapshot()

        try:
            from automation.browser_driver import browser_switch_tab
            result = browser_switch_tab(index=index, title=title)
            time.sleep(0.3)
            after = self._snapshot()
            ok = "not found" not in result.lower()
            return ActionResult(
                ok=ok, strategy_used="cdp" if self._obs.is_cdp_available() else "keyboard",
                state_before=before, state_after=after,
                verified=before.get("title") != after.get("title"),
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def new_tab(self, url=None):
        """Open a new browser tab."""
        before = self._snapshot()

        try:
            from automation.browser_driver import browser_new_tab
            result = browser_new_tab(url)
            time.sleep(0.5)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="keyboard",
                state_before=before, state_after=after,
                verified=after.get("tab_count", 0) > before.get("tab_count", 0),
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def close_tab(self):
        """Close the current browser tab."""
        before = self._snapshot()

        try:
            from automation.browser_driver import browser_close_tab
            result = browser_close_tab()
            time.sleep(0.3)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="keyboard",
                state_before=before, state_after=after,
                verified=after.get("tab_count", 0) < before.get("tab_count", 0),
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))

    def go_back(self):
        """Navigate back."""
        before = self._snapshot()
        try:
            from automation.browser_driver import browser_back
            result = browser_back()
            time.sleep(0.5)
            after = self._snapshot()
            return ActionResult(
                ok=True, strategy_used="keyboard",
                state_before=before, state_after=after,
                verified=before.get("url") != after.get("url"),
                message=result,
            )
        except Exception as e:
            return ActionResult(ok=False, state_before=before, error=str(e))
