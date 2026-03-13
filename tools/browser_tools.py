"""Browser automation tools via Playwright / CDP.

Provides a persistent-session browser_action tool that uses Playwright
(preferred) or CDPSession for reliable, stateful browser control. This
is an enhanced alternative to the existing browser_driver.py which opens
a new WebSocket per command.

The tool is registered as non-core (cloud providers only) since it adds
another action enum that local models may struggle with.
"""

import logging

logger = logging.getLogger(__name__)

# Try Playwright first — better selectors, auto-wait, cross-browser
try:
    from automation.playwright_session import get_playwright_session
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


def _handle_browser_action(arguments=None, **kwargs):
    """Handle browser_action tool calls from the LLM.

    Actions: navigate, click, fill, read, screenshot, get_url, get_tabs,
             switch_tab, new_tab, close_tab, back, forward, find_text, run_js, wait_for

    Tries Playwright first, falls back to CDPSession.
    """
    if not arguments:
        return "No arguments provided"

    action = arguments.get("action", "").lower()
    if not action:
        return "No action specified"

    # Try Playwright first
    if _HAS_PLAYWRIGHT:
        try:
            return _handle_via_playwright(action, arguments)
        except Exception as e:
            logger.debug(f"Playwright browser_action failed ({action}), falling back to CDP: {e}")

    # Fall back to CDP
    return _handle_via_cdp(action, arguments)


def _handle_via_playwright(action, arguments):
    """Execute a browser action using Playwright session.

    Raises exceptions on failure so caller can fall back to CDP.
    """
    session = get_playwright_session()
    session.connect()

    if action == "navigate":
        url = arguments.get("url", "")
        if not url:
            return "No URL specified"
        session.navigate(url)
        title = session.get_title()
        return f"Navigated to {url} — {title}"

    elif action == "click":
        selector = arguments.get("selector", "")
        text_val = arguments.get("text", "")
        if not selector and not text_val:
            return "No CSS selector or text specified"
        session.click(selector=selector or None, text=text_val or None)
        return f"Clicked: {selector or text_val}"

    elif action == "fill":
        selector = arguments.get("selector", "")
        text_val = arguments.get("text", "")
        if not selector:
            return "No CSS selector specified"
        session.fill(selector, text_val)
        return f"Filled {selector} with text"

    elif action == "read":
        selector = arguments.get("selector")
        text = session.get_text(selector)
        if not text:
            return "Page is empty or not loaded"
        return text[:2000]

    elif action == "screenshot":
        import os
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "pw_screenshot.png")
        session.screenshot_to_file(path)
        return f"Screenshot saved to {path}"

    elif action == "get_url":
        return session.get_url()

    elif action == "get_tabs":
        tabs = session.get_tabs()
        if not tabs:
            return "No tabs open"
        lines = [f"  {i+1}. {t['title'][:50]} — {t['url'][:60]}" for i, t in enumerate(tabs)]
        return f"Open tabs ({len(tabs)}):\n" + "\n".join(lines)

    elif action == "switch_tab":
        idx = arguments.get("selector", "")  # tab index passed via selector
        try:
            idx = int(idx)
        except (ValueError, TypeError):
            idx = 0
        session.switch_tab(idx)
        return f"Switched to tab {idx}"

    elif action == "new_tab":
        url = arguments.get("url", "about:blank")
        tab_id = session.new_tab(url)
        return f"New tab opened: {tab_id}"

    elif action == "close_tab":
        session.close_tab()
        return "Tab closed"

    elif action == "back":
        session.back()
        return "Navigated back"

    elif action == "forward":
        session.forward()
        return "Navigated forward"

    elif action == "find_text":
        text = arguments.get("text", "")
        found = session.find_text(text)
        return f"Text {'found' if found else 'not found'} on page"

    elif action == "run_js":
        expression = arguments.get("expression", arguments.get("text", ""))
        result = session.run_js(expression)
        return str(result) if result is not None else "JavaScript executed (no return value)"

    elif action == "wait_for":
        selector = arguments.get("selector", "")
        timeout = arguments.get("timeout", 10000)
        if isinstance(timeout, (int, float)) and timeout < 1000:
            timeout = timeout * 1000  # Convert seconds to ms if needed
        session.wait_for_element(selector, timeout=int(timeout))
        return f"Element {selector} found"

    else:
        return f"Unknown browser action: {action}"


def _handle_via_cdp(action, arguments):
    """Execute a browser action using the CDP session (fallback)."""
    try:
        from automation.cdp_session import get_cdp_session
        session = get_cdp_session()

        # Ensure Chrome is running and connected
        if not session._connected:
            if not session.ensure_chrome():
                return "Could not connect to Chrome. Make sure Chrome is installed."
            session.connect()

        if action == "navigate":
            url = arguments.get("url", "")
            if not url:
                return "No URL specified"
            session.navigate(url)
            title = session.get_title()
            return f"Navigated to {url} — {title}"

        elif action == "click":
            selector = arguments.get("selector", "")
            if not selector:
                return "No CSS selector specified"
            session.click(selector)
            return f"Clicked: {selector}"

        elif action == "fill":
            selector = arguments.get("selector", "")
            text = arguments.get("text", "")
            if not selector:
                return "No CSS selector specified"
            session.fill(selector, text)
            return f"Filled {selector} with text"

        elif action == "read":
            selector = arguments.get("selector")
            text = session.get_text(selector)
            if not text:
                return "Page is empty or not loaded"
            return text[:2000]  # Limit for LLM context

        elif action == "screenshot":
            import os
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "cdp_screenshot.png")
            session.screenshot_to_file(path)
            return f"Screenshot saved to {path}"

        elif action == "get_url":
            return session.get_url()

        elif action == "get_tabs":
            tabs = session.get_tabs()
            if not tabs:
                return "No tabs open"
            lines = [f"  {i+1}. {t['title'][:50]} — {t['url'][:60]}" for i, t in enumerate(tabs)]
            return f"Open tabs ({len(tabs)}):\n" + "\n".join(lines)

        elif action == "new_tab":
            url = arguments.get("url", "about:blank")
            tab_id = session.new_tab(url)
            return f"New tab opened: {tab_id}"

        elif action == "close_tab":
            session.close_tab()
            return "Tab closed"

        elif action == "back":
            session.back()
            return "Navigated back"

        elif action == "forward":
            session.forward()
            return "Navigated forward"

        elif action == "find_text":
            text = arguments.get("text", "")
            found = session.find_text(text)
            return f"Text {'found' if found else 'not found'} on page"

        elif action == "run_js":
            expression = arguments.get("expression", arguments.get("text", ""))
            result = session.run_js(expression)
            return str(result) if result is not None else "JavaScript executed (no return value)"

        elif action == "wait_for":
            selector = arguments.get("selector", "")
            timeout = arguments.get("timeout", 10)
            session.wait_for_element(selector, timeout=timeout)
            return f"Element {selector} found"

        else:
            return f"Unknown browser action: {action}"

    except ImportError:
        return "websocket-client not installed. Run: pip install websocket-client"
    except ConnectionError as e:
        return f"Chrome connection error: {e}"
    except ValueError as e:
        return str(e)
    except TimeoutError as e:
        return str(e)
    except Exception as e:
        logger.error(f"Browser action error: {e}")
        return f"Browser error: {e}"


def register_browser_tools(registry):
    """Register browser tools with the tool registry."""
    try:
        from tools.schemas import ToolSpec

        spec = ToolSpec(
            name="browser_action",
            description=(
                "Control Chrome browser directly. Actions: navigate (url), click (selector), "
                "fill (selector, text), read (get page text), screenshot, get_url, get_tabs, "
                "new_tab, close_tab, back, forward, find_text (text), run_js (expression), "
                "wait_for (selector). More reliable than clicking screen pixels."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["navigate", "click", "fill", "read", "screenshot",
                                 "get_url", "get_tabs", "new_tab", "close_tab",
                                 "back", "forward", "find_text", "run_js", "wait_for"],
                        "description": "The browser action to perform"
                    },
                    "url": {"type": "string", "description": "URL for navigate/new_tab"},
                    "selector": {"type": "string", "description": "CSS selector for click/fill/read/wait_for"},
                    "text": {"type": "string", "description": "Text for fill/find_text"},
                    "expression": {"type": "string", "description": "JavaScript for run_js"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds for wait_for"},
                },
                "required": ["action"],
            },
            handler=_handle_browser_action,
            aliases=["browser", "chrome", "web_action", "browse", "open_url",
                     "webpage", "browser_click", "browser_fill",
                     "browser_read", "browser_navigate", "navigate",
                     "go_to_url", "page_content"],
            safety="moderate",
            arg_aliases={"target": "selector", "element": "selector", "name": "selector",
                         "query": "text", "search": "text", "value": "text",
                         "link": "url", "page": "url", "site": "url",
                         "tab_index": "selector"},
            primary_arg="action",
            core=False,  # Not in Ollama's reduced tool set — use for cloud providers
        )
        registry.register(spec)
        logger.info("Registered browser_action tool (CDP persistent session)")
    except Exception as e:
        logger.debug(f"Could not register browser tools: {e}")
