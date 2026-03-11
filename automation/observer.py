"""
Screen observation: screenshots, vision analysis, window state.

Extracted from desktop_agent.py: _observe(), _get_window_inventory(),
_get_running_apps(), _get_browser_url(), _extract_browser_content().

Responsibility:
  - OS-level window/process enumeration (fast, no AI)
  - Screenshot + llava vision analysis (slow, contextual)
  - Smart vision skip for non-visual tool results
  - Browser URL and content extraction
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

# Tools whose results don't change the visible screen
_NON_VISUAL_TOOLS = {
    "run_terminal", "manage_files", "manage_software", "get_weather",
    "get_time", "get_news", "get_forecast", "set_reminder",
    "list_reminders", "web_read", "web_search_answer", "create_file",
    "run_command",
}

# Phrases indicating a real screen blocker
_BLOCKER_PHRASES = [
    "popup", "dialog box", "modal",
    "profile picker", "choose profile", "select profile",
    "default browser", "not your default", "set as default",
    "cookie banner", "cookie consent", "accept cookies",
    "sign in required", "login required",
    "choose an app", "how do you want to open",
    "overlay", "blocking",
]

# False positives — not real blockers
_NOT_BLOCKERS = [
    "terminal", "command prompt", "powershell", "cmd",
    "desktop", "taskbar", "start menu",
]


class ScreenObserver:
    """Captures and analyzes screen state for the desktop agent."""

    def observe(self, goal, history):
        """Build comprehensive screen state.

        Args:
            goal: Current goal text (for context).
            history: Action history list (to decide vision skip).

        Returns dict with: summary, blocked, foreground, windows,
        processes, image, image_b64, browser_content, ui_elements.
        """
        from vision import (
            capture_screenshot, image_to_base64, _call_llava,
            get_active_window_title,
        )

        image = capture_screenshot()
        if image is None:
            return {
                "summary": "Could not capture screenshot", "blocked": False,
                "foreground": "unknown", "raw": "", "windows": [],
                "processes": [], "image": None, "image_b64": None,
                "browser_content": None, "ui_elements": [],
            }

        window_title = get_active_window_title()
        visible_windows = self.get_window_inventory()
        running_apps = self.get_running_apps()
        b64 = image_to_base64(image)

        os_summary = f"Active window: {window_title}"
        if visible_windows:
            win_list = [w[:50] for w in visible_windows[:5]]
            os_summary += f"\nVisible windows: {', '.join(win_list)}"

        use_vision = self._should_use_vision(history)

        if not use_vision:
            description = os_summary
        else:
            prompt = (
                "Describe this Windows screenshot in 1-2 SHORT sentences:\n"
                "- What is the main app/window visible?\n"
                "- Is there a popup, dialog box, or modal OVERLAYING the main window?\n"
                "IMPORTANT: A terminal/command prompt/PowerShell window is NOT a blocker.\n"
                "Only report popups/dialogs that are clearly blocking something else."
            )
            vision_desc = _call_llava(prompt, b64, temperature=0.1, num_predict=150)
            description = (f"{os_summary}\nVision: {vision_desc}"
                           if vision_desc else os_summary)

        if description is None:
            description = "Vision model did not respond."

        blocked = self._detect_blockers(description)

        # Extract browser content when in a browser
        browser_content = None
        browser_keywords = ["firefox", "chrome", "edge", "brave", "opera"]
        if any(b in (window_title or "").lower() for b in browser_keywords):
            browser_content = self.extract_browser_content()
            if browser_content and browser_content.get("content"):
                description += f"\nPage URL: {browser_content.get('url', 'unknown')}"
                description += (f"\nPage content preview: "
                                f"{browser_content['content'][:300]}")

        # Get UI Automation elements
        ui_elements = []
        try:
            from computer import get_ui_elements
            ui_elements = get_ui_elements(max_depth=3, max_elements=20)
        except Exception:
            pass

        return {
            "summary": description.strip(),
            "blocked": blocked,
            "foreground": window_title,
            "windows": visible_windows,
            "processes": running_apps,
            "raw": description,
            "image": image,
            "image_b64": b64,
            "browser_content": browser_content,
            "ui_elements": ui_elements,
        }

    @staticmethod
    def get_window_inventory():
        """Get all visible windows with titles (fast OS-level)."""
        try:
            import pygetwindow as gw
            return [w.title for w in gw.getAllWindows()
                    if w.title and w.visible and not w.isMinimized]
        except Exception:
            return []

    @staticmethod
    def get_running_apps():
        """Get key running processes (user-facing apps)."""
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "STATUS eq Running", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            _SKIP = {
                "svchost.exe", "csrss.exe", "wininit.exe", "services.exe",
                "lsass.exe", "dwm.exe", "conhost.exe", "RuntimeBroker.exe",
                "tasklist.exe", "cmd.exe", "python.exe", "python3.12.exe",
            }
            apps = set()
            for line in result.stdout.strip().split("\n"):
                parts = line.strip('"').split('","')
                if parts and parts[0] not in _SKIP:
                    apps.add(parts[0])
            return sorted(apps)[:20]
        except Exception:
            return []

    @staticmethod
    def get_browser_url():
        """Get current URL from active browser window."""
        try:
            import pygetwindow as gw
            import pyautogui

            active = gw.getActiveWindow()
            if not active or not active.title:
                return None

            title_lower = active.title.lower()
            browser_keywords = ["firefox", "chrome", "edge", "brave", "opera"]
            if not any(b in title_lower for b in browser_keywords):
                return None

            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.2)
            pyautogui.press("escape")

            try:
                import subprocess
                result = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=5,
                )
                url = result.stdout.strip()
                if url.startswith(("http://", "https://")):
                    return url
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Could not get browser URL: {e}")
        return None

    def extract_browser_content(self):
        """Extract structured content from browser page.

        Returns dict with url, title, links, content, forms.
        """
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if not active or not active.title:
                return None

            title = active.title
            title_lower = title.lower()
            browser_keywords = ["firefox", "chrome", "edge", "brave", "opera"]
            if not any(b in title_lower for b in browser_keywords):
                return None

            url = self.get_browser_url()
            if not url:
                return {"title": title, "url": None, "content": None, "links": []}

            try:
                from web_agent import web_read
                import requests as _req

                raw_content = ""
                links = []
                forms = []
                try:
                    resp = _req.get(url, headers={
                        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36")
                    }, timeout=5)
                    html = resp.text

                    # Extract links
                    link_pattern = re.compile(
                        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                        re.DOTALL)
                    for href, text in link_pattern.findall(html):
                        text_clean = re.sub(r'<[^>]+>', '', text).strip()
                        if text_clean and 2 < len(text_clean) < 200:
                            links.append({"text": text_clean[:80],
                                          "href": href[:200]})

                    # Extract buttons
                    btn_pattern = re.compile(
                        r'<button[^>]*>(.*?)</button>', re.DOTALL)
                    for btn_text in btn_pattern.findall(html):
                        btn_clean = re.sub(r'<[^>]+>', '', btn_text).strip()
                        if btn_clean and len(btn_clean) > 1:
                            links.append({"text": f"[Button] {btn_clean[:60]}",
                                          "href": ""})

                    # Extract input fields
                    input_pattern = re.compile(
                        r'<input[^>]+(?:placeholder|aria-label)='
                        r'["\']([^"\']+)["\']', re.I)
                    for placeholder in input_pattern.findall(html):
                        links.append({"text": f"[Input] {placeholder[:60]}",
                                      "href": ""})

                    # Detect forms
                    form_pattern = re.compile(
                        r'<form[^>]*>(.*?)</form>', re.DOTALL | re.I)
                    for form_html in form_pattern.findall(html):
                        form_fields = []
                        label_pattern = re.compile(
                            r'<label[^>]*>([^<]+)</label>\s*'
                            r'(?:<input[^>]+(?:name|id)=["\']([^"\']+)["\'])?',
                            re.I)
                        for label, name in label_pattern.findall(form_html):
                            form_fields.append({"label": label.strip(),
                                                "name": name or label.strip()})

                        inp_pattern = re.compile(
                            r'<input[^>]+(?:placeholder|aria-label)='
                            r'["\']([^"\']+)["\'][^>]*'
                            r'(?:name=["\']([^"\']+)["\'])?', re.I)
                        for placeholder, name in inp_pattern.findall(form_html):
                            form_fields.append({
                                "label": placeholder.strip(),
                                "name": name or placeholder.strip()})

                        submit_pattern = re.compile(
                            r'<(?:button|input)[^>]+(?:type=["\']submit["\']|'
                            r'type=["\']button["\'])[^>]*'
                            r'(?:value=["\']([^"\']+)["\'])?[^>]*>([^<]*)',
                            re.I)
                        for value, text in submit_pattern.findall(form_html):
                            btn_text = (value or text or "Submit").strip()
                            if btn_text:
                                form_fields.append({
                                    "label": f"[Submit] {btn_text}",
                                    "name": "__submit__"})

                        if form_fields:
                            forms.append(form_fields)

                except Exception as e:
                    logger.debug(f"Page fetch error: {e}")

                page_text = web_read(url)
                if page_text and len(page_text) > 50:
                    raw_content = page_text[:1000]

                # Deduplicate links
                seen = set()
                unique_links = []
                for lnk in links:
                    key = lnk["text"].lower()
                    if key not in seen:
                        seen.add(key)
                        unique_links.append(lnk)
                    if len(unique_links) >= 20:
                        break

                return {
                    "title": title, "url": url, "content": raw_content,
                    "links": unique_links, "forms": forms,
                }
            except Exception as e:
                logger.debug(f"Browser content extraction failed: {e}")
                return {"title": title, "url": url, "content": None, "links": []}

        except Exception as e:
            logger.debug(f"Browser content extraction error: {e}")
            return None

    @staticmethod
    def _should_use_vision(history):
        """Decide whether to use llava vision (skip after non-visual tools)."""
        if not history:
            return True
        last_tool = history[-1].get("tool", "")
        if last_tool in _NON_VISUAL_TOOLS:
            logger.info(f"Skipping vision (last action was non-visual: {last_tool})")
            return False
        return True

    @staticmethod
    def _detect_blockers(description):
        """Detect real screen blockers from observation text."""
        desc_lower = description.lower()
        for kw in _BLOCKER_PHRASES:
            if kw in desc_lower:
                if not any(nb in desc_lower for nb in _NOT_BLOCKERS):
                    return True
        return False
