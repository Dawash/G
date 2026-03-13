"""
Browser automation via Chrome DevTools Protocol (CDP) + UIA + keyboard fallback.

Phase 17: Provides programmatic browser control without vision/screenshots.

Resolution hierarchy:
  1. CDP (if Chrome/Edge launched with --remote-debugging-port=9222)
  2. UIA (browser accessibility tree — address bar, tabs, bookmarks)
  3. Keyboard shortcuts (Ctrl+L, Ctrl+F, Ctrl+T, etc.)

CDP gives full DOM access: navigate, click by selector, fill fields, read page
content, run JavaScript. Falls back gracefully when CDP isn't available.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

# CDP connection settings
_CDP_HOST = "localhost"
_CDP_PORT = 9222
_CDP_TIMEOUT = 5

# Cached connection state
_cdp_available = None
_last_cdp_check = 0
_CDP_CHECK_INTERVAL = 30  # Re-check every 30s


# ===================================================================
# CDP connection
# ===================================================================

def _check_cdp():
    """Check if Chrome DevTools Protocol is available."""
    global _cdp_available, _last_cdp_check

    now = time.time()
    if _cdp_available is not None and (now - _last_cdp_check) < _CDP_CHECK_INTERVAL:
        return _cdp_available

    _last_cdp_check = now
    try:
        import http.client
        conn = http.client.HTTPConnection(_CDP_HOST, _CDP_PORT, timeout=2)
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        if resp.status == 200:
            _cdp_available = True
            return True
        conn.close()
    except Exception:
        pass

    _cdp_available = False
    return False


def _get_tabs():
    """Get list of browser tabs via CDP."""
    try:
        import http.client
        conn = http.client.HTTPConnection(_CDP_HOST, _CDP_PORT, timeout=_CDP_TIMEOUT)
        conn.request("GET", "/json")
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            conn.close()
            return [t for t in data if t.get("type") == "page"]
        conn.close()
    except Exception as e:
        logger.debug(f"CDP get_tabs error: {e}")
    return []


def _send_cdp_command(ws_url, method, params=None):
    """Send a command to a tab via CDP WebSocket.

    Returns the result dict or None on error.
    """
    try:
        from websocket import create_connection
    except ImportError:
        logger.debug("websocket-client not installed, CDP commands unavailable")
        return None

    try:
        ws = create_connection(ws_url, timeout=_CDP_TIMEOUT)
        msg = {"id": 1, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        result = json.loads(ws.recv())
        ws.close()
        return result.get("result")
    except Exception as e:
        logger.debug(f"CDP command error ({method}): {e}")
        return None


def _get_active_tab_ws():
    """Get the WebSocket URL for the active tab."""
    tabs = _get_tabs()
    if not tabs:
        return None
    # First tab is usually the active one
    return tabs[0].get("webSocketDebuggerUrl")


# ===================================================================
# Public API — browser actions
# ===================================================================

def browser_navigate(url):
    """Navigate the active tab to a URL.

    Falls back to opening URL in default browser if CDP unavailable.
    """
    if not url:
        return "No URL provided."

    # Ensure URL has scheme
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url

    # Try CDP
    if _check_cdp():
        ws = _get_active_tab_ws()
        if ws:
            result = _send_cdp_command(ws, "Page.navigate", {"url": url})
            if result:
                return f"Navigated to {url}"

    # Fallback: keyboard shortcut (Ctrl+L, type URL, Enter)
    try:
        from automation.ui_control import find_window

        # Check if a browser is active
        browser_win = None
        for name in ("Chrome", "Edge", "Firefox", "Brave"):
            browser_win = find_window(name)
            if browser_win:
                break

        if browser_win:
            try:
                browser_win.set_focus()
                time.sleep(0.2)
            except Exception:
                pass
            import pyautogui
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)

            import pyperclip
            pyperclip.copy(url)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.1)
            pyautogui.press("enter")
            return f"Navigating to {url}"
    except Exception as e:
        logger.debug(f"Keyboard navigate fallback error: {e}")

    # Last resort: open in default browser
    try:
        import webbrowser
        webbrowser.open(url)
        return f"Opened {url} in default browser."
    except Exception as e:
        return f"Failed to navigate: {e}"


def browser_click(selector=None, text=None):
    """Click an element in the active browser tab.

    Args:
        selector: CSS selector (e.g. '#submit', '.btn-primary', 'a[href="/login"]').
        text: Visible text to find and click (used if selector not provided).

    Returns:
        str: Result message.
    """
    if not selector and not text:
        return "Provide a CSS selector or visible text to click."

    # Try CDP
    if _check_cdp():
        ws = _get_active_tab_ws()
        if ws:
            if selector:
                js = f"""
                (() => {{
                    const el = document.querySelector('{_js_escape(selector)}');
                    if (!el) return 'NOT_FOUND';
                    el.click();
                    return 'CLICKED: ' + (el.textContent || el.tagName).substring(0, 50);
                }})()
                """
            else:
                js = f"""
                (() => {{
                    const text = '{_js_escape(text)}';
                    const lower = text.toLowerCase();
                    // Search clickable elements
                    const candidates = [...document.querySelectorAll('a, button, input[type="submit"], input[type="button"], [role="button"], [onclick]')];
                    for (const el of candidates) {{
                        const content = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
                        if (content.toLowerCase().includes(lower)) {{
                            el.click();
                            return 'CLICKED: ' + content.substring(0, 50);
                        }}
                    }}
                    return 'NOT_FOUND';
                }})()
                """
            result = _send_cdp_command(ws, "Runtime.evaluate",
                                       {"expression": js, "returnByValue": True})
            if result:
                value = result.get("result", {}).get("value", "")
                if value and value != "NOT_FOUND":
                    return value
                if value == "NOT_FOUND":
                    return f"Element '{selector or text}' not found in page."

    # Fallback: UIA (browser controls) or find via accessibility
    target = text or selector
    try:
        from automation.ui_control import click_control
        return click_control(name=target)
    except Exception as e:
        return f"Could not click '{target}': {e}"


def browser_fill(selector=None, text="", field_name=None):
    """Fill a form field in the active browser tab.

    Args:
        selector: CSS selector for the input field.
        text: Text to fill.
        field_name: Human-readable field name (used if selector not provided).

    Returns:
        str: Result message.
    """
    if not text:
        return "No text to fill."
    if not selector and not field_name:
        return "Provide a CSS selector or field name."

    # Try CDP
    if _check_cdp():
        ws = _get_active_tab_ws()
        if ws:
            if selector:
                js = f"""
                (() => {{
                    const el = document.querySelector('{_js_escape(selector)}');
                    if (!el) return 'NOT_FOUND';
                    el.focus();
                    el.value = '{_js_escape(text)}';
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return 'FILLED: ' + (el.name || el.id || el.type || 'input');
                }})()
                """
            else:
                js = f"""
                (() => {{
                    const name = '{_js_escape(field_name)}';
                    const lower = name.toLowerCase();
                    const inputs = [...document.querySelectorAll('input, textarea, select, [contenteditable]')];
                    for (const el of inputs) {{
                        const label = (el.getAttribute('placeholder') || el.getAttribute('aria-label') ||
                                      el.getAttribute('name') || el.id || '').toLowerCase();
                        // Also check associated label element
                        let labelText = '';
                        if (el.id) {{
                            const labelEl = document.querySelector('label[for="' + el.id + '"]');
                            if (labelEl) labelText = labelEl.textContent.toLowerCase();
                        }}
                        if (label.includes(lower) || labelText.includes(lower)) {{
                            el.focus();
                            el.value = '{_js_escape(text)}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return 'FILLED: ' + (el.name || el.id || el.type || 'input');
                        }}
                    }}
                    return 'NOT_FOUND';
                }})()
                """
            result = _send_cdp_command(ws, "Runtime.evaluate",
                                       {"expression": js, "returnByValue": True})
            if result:
                value = result.get("result", {}).get("value", "")
                if value and value != "NOT_FOUND":
                    return value
                if value == "NOT_FOUND":
                    return f"Field '{selector or field_name}' not found in page."

    # Fallback: UIA
    target = field_name or selector
    try:
        from automation.ui_control import set_control_text
        return set_control_text(name=target, text=text)
    except Exception as e:
        return f"Could not fill '{target}': {e}"


def browser_read(selector=None):
    """Read text content from the active browser tab.

    Args:
        selector: CSS selector to read (None = entire page body).

    Returns:
        str: Page text content (truncated to 3000 chars).
    """
    # Try CDP
    if _check_cdp():
        ws = _get_active_tab_ws()
        if ws:
            if selector:
                js = f"""
                (() => {{
                    const el = document.querySelector('{_js_escape(selector)}');
                    return el ? el.innerText.substring(0, 3000) : 'NOT_FOUND';
                }})()
                """
            else:
                js = """
                (() => {
                    // Get main content, preferring article/main over full body
                    const article = document.querySelector('article, main, [role="main"]');
                    const el = article || document.body;
                    if (!el) return '';
                    // Remove scripts, styles, nav, footer
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('script, style, nav, footer, header, aside, [role="navigation"]')
                         .forEach(e => e.remove());
                    return clone.innerText.substring(0, 3000);
                })()
                """
            result = _send_cdp_command(ws, "Runtime.evaluate",
                                       {"expression": js, "returnByValue": True})
            if result:
                value = result.get("result", {}).get("value", "")
                if value and value != "NOT_FOUND":
                    return value

    # Fallback: try to read from address bar and use web_agent
    try:
        url = browser_get_url()
        if url and url.startswith("http"):
            from web_agent import web_read
            return web_read(url)
    except Exception:
        pass

    return "Could not read page content. CDP not available and no URL detected."


def browser_run_js(script):
    """Execute JavaScript in the active browser tab.

    Args:
        script: JavaScript code to execute.

    Returns:
        str: Result of the script, or error message.
    """
    if not script:
        return "No script provided."

    # Safety check
    dangerous = ["window.close", "document.cookie", "localStorage.clear",
                 "indexedDB.deleteDatabase", "fetch(", "XMLHttpRequest"]
    script_lower = script.lower()
    for d in dangerous:
        if d.lower() in script_lower:
            return f"Blocked: script contains '{d}' which is restricted."

    if not _check_cdp():
        return "CDP not available. Start Chrome with --remote-debugging-port=9222"

    ws = _get_active_tab_ws()
    if not ws:
        return "No active browser tab found."

    result = _send_cdp_command(ws, "Runtime.evaluate",
                               {"expression": script, "returnByValue": True})
    if result:
        value = result.get("result", {}).get("value")
        if value is not None:
            return str(value)[:2000]
        desc = result.get("result", {}).get("description", "")
        if desc:
            return desc[:2000]
        return "Script executed (no return value)."

    return "Failed to execute script."


def browser_get_url():
    """Get the current URL from the active browser tab.

    Returns:
        str: Current URL, or empty string if unavailable.
    """
    # Try CDP
    if _check_cdp():
        tabs = _get_tabs()
        if tabs:
            return tabs[0].get("url", "")

    # Fallback: read address bar via UIA
    try:
        from automation.ui_control import find_control, get_active_window_info

        info = get_active_window_info()
        if not info:
            return ""

        title = info.get("title", "").lower()
        is_browser = any(b in title for b in
                        ("chrome", "edge", "firefox", "brave", "opera", "vivaldi"))
        if not is_browser:
            return ""

        # Try to find address bar control
        for auto_id in ("addressEditBox", "urlbar-input", "view.addressView"):
            ctrl = find_control(automation_id=auto_id, window=info["title"])
            if ctrl and ctrl.get("name"):
                return ctrl["name"]

        # Try by role
        ctrl = find_control(name="Address and search bar", window=info["title"])
        if ctrl and ctrl.get("name"):
            return ctrl["name"]

    except Exception as e:
        logger.debug(f"URL detection error: {e}")

    return ""


def browser_get_tabs():
    """List open browser tabs.

    Returns:
        list of dicts with title and url.
    """
    # Try CDP
    if _check_cdp():
        tabs = _get_tabs()
        return [{"title": t.get("title", ""), "url": t.get("url", "")}
                for t in tabs]

    # Fallback: keyboard Ctrl+Tab cycling isn't practical
    # Try UIA tab strip
    try:
        from automation.ui_control import list_controls, find_window

        for browser_name in ("Chrome", "Edge", "Firefox"):
            win = find_window(browser_name)
            if win:
                ctrls = list_controls(window=browser_name, role="TabItem",
                                      max_count=20)
                if ctrls:
                    return [{"title": c["name"], "url": ""} for c in ctrls]
    except Exception:
        pass

    return []


def browser_switch_tab(index=None, title=None):
    """Switch to a browser tab by index or title.

    Args:
        index: 0-based tab index.
        title: Partial title match.

    Returns:
        str: Result message.
    """
    # Try CDP
    if _check_cdp():
        tabs = _get_tabs()
        if title:
            title_lower = title.lower()
            for tab in tabs:
                if title_lower in tab.get("title", "").lower():
                    ws = tab.get("webSocketDebuggerUrl")
                    if ws:
                        _send_cdp_command(ws, "Page.bringToFront")
                        return f"Switched to tab: {tab['title'][:50]}"
        elif index is not None and 0 <= index < len(tabs):
            ws = tabs[index].get("webSocketDebuggerUrl")
            if ws:
                _send_cdp_command(ws, "Page.bringToFront")
                return f"Switched to tab {index}: {tabs[index].get('title', '')[:50]}"
        return "Tab not found."

    # Fallback: Ctrl+<number> for tabs 1-9
    if index is not None and 0 <= index < 9:
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", str(index + 1))
            return f"Switched to tab {index + 1}."
        except Exception:
            pass

    return "Could not switch tab."


def browser_new_tab(url=None):
    """Open a new browser tab, optionally navigating to a URL."""
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "t")
        time.sleep(0.3)
        if url:
            return browser_navigate(url)
        return "Opened new tab."
    except Exception as e:
        return f"Failed to open new tab: {e}"


def browser_close_tab():
    """Close the current browser tab."""
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "w")
        return "Closed current tab."
    except Exception as e:
        return f"Failed to close tab: {e}"


def browser_back():
    """Navigate back in the browser."""
    try:
        import pyautogui
        pyautogui.hotkey("alt", "left")
        return "Navigated back."
    except Exception as e:
        return f"Failed: {e}"


def browser_forward():
    """Navigate forward in the browser."""
    try:
        import pyautogui
        pyautogui.hotkey("alt", "right")
        return "Navigated forward."
    except Exception as e:
        return f"Failed: {e}"


def browser_find_text(text):
    """Use browser's Find feature to locate text on page."""
    if not text:
        return "No text to find."
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.3)
        import pyperclip
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)
        return f"Searching for '{text}' on page."
    except Exception as e:
        return f"Failed: {e}"


def browser_snapshot():
    """Get a structured snapshot of the current browser page.

    Returns dict with: url, title, links, inputs, buttons.
    Useful for the agent to understand page structure without vision.
    """
    snapshot = {"url": "", "title": "", "links": [], "inputs": [], "buttons": []}

    # Try CDP
    if _check_cdp():
        ws = _get_active_tab_ws()
        if ws:
            tabs = _get_tabs()
            if tabs:
                snapshot["url"] = tabs[0].get("url", "")
                snapshot["title"] = tabs[0].get("title", "")

            js = """
            (() => {
                const result = {links: [], inputs: [], buttons: []};

                // Links (first 15)
                const links = document.querySelectorAll('a[href]');
                for (let i = 0; i < Math.min(links.length, 15); i++) {
                    const a = links[i];
                    const text = (a.textContent || '').trim().substring(0, 60);
                    if (text) result.links.push({text, href: a.href.substring(0, 100)});
                }

                // Inputs (first 10)
                const inputs = document.querySelectorAll('input, textarea, select');
                for (let i = 0; i < Math.min(inputs.length, 10); i++) {
                    const inp = inputs[i];
                    result.inputs.push({
                        type: inp.type || inp.tagName.toLowerCase(),
                        name: inp.name || inp.id || inp.getAttribute('placeholder') || '',
                        value: (inp.value || '').substring(0, 50),
                    });
                }

                // Buttons (first 10)
                const btns = document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]');
                for (let i = 0; i < Math.min(btns.length, 10); i++) {
                    const btn = btns[i];
                    result.buttons.push({
                        text: (btn.textContent || btn.value || '').trim().substring(0, 40),
                    });
                }

                return JSON.stringify(result);
            })()
            """
            result = _send_cdp_command(ws, "Runtime.evaluate",
                                       {"expression": js, "returnByValue": True})
            if result:
                value = result.get("result", {}).get("value", "")
                if value:
                    try:
                        parsed = json.loads(value)
                        snapshot.update(parsed)
                    except json.JSONDecodeError:
                        pass

    return snapshot


def is_browser_active():
    """Check if a browser window is currently in the foreground."""
    try:
        from automation.ui_control import get_active_window_info
        info = get_active_window_info()
        if not info:
            return False
        title = info.get("title", "").lower()
        proc = info.get("process_name", "").lower()
        browsers = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")
        return any(b in title or b in proc for b in browsers)
    except Exception:
        return False


def is_cdp_available():
    """Check if Chrome DevTools Protocol is accessible."""
    return _check_cdp()


# ===================================================================
# Helpers
# ===================================================================

def _js_escape(s):
    """Escape a string for safe JavaScript string interpolation."""
    return (s.replace("\\", "\\\\")
             .replace("'", "\\'")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r"))
