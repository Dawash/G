"""
Desktop tool registrations — automation, media, vision, file creation.

Registers: play_music, search_in_app, create_file, type_text, press_key,
           click_at, scroll, take_screenshot, find_on_screen, click_element,
           manage_tabs, fill_form, focus_window, click_control,
           inspect_window, set_control_text, snap_window, list_windows,
           agent_task
"""

import logging
import re

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ===================================================================
# Handler functions
# ===================================================================

def _handle_play_music(arguments, user_input="", quick_chat_fn=None):
    """Play music with query injection from user input if LLM forgot it."""
    if arguments.get("action") in ("play", "play_query", None) and not arguments.get("query"):
        if user_input:
            # Skip query injection for resume/pause commands (no specific song requested)
            _is_resume = re.search(r'^(resume|continue|unpause|pause|stop)\s', user_input, re.I)
            _is_generic = re.search(r'^(play|start)\s+(the\s+)?music$', user_input, re.I)
            if not _is_resume and not _is_generic:
                cleaned = re.sub(r'^(play|listen to|put on|start)\s+', '', user_input, flags=re.I)
                cleaned = re.sub(r'\s+(on|in|using|with|from)\s+(spotify|youtube|browser).*$', '', cleaned, flags=re.I)
                cleaned = re.sub(r'^(some|a|the|my|me)\s+', '', cleaned, flags=re.I)
                cleaned = cleaned.strip()
                if cleaned and len(cleaned) > 1 and cleaned.lower() not in ('music', 'song', 'track'):
                    arguments["query"] = cleaned
                    logger.info(f"play_music: injected missing query '{cleaned}' from user input")
    action = arguments.get("action", "play")
    query = arguments.get("query", "")
    app = arguments.get("app", "spotify")
    from platform_impl.windows.media import play_music
    return play_music(action, query, app,
                      last_user_input=user_input or "",
                      quick_chat_fn=quick_chat_fn)


def _handle_search_in_app(arguments):
    from computer import search_in_app
    return search_in_app(
        arguments.get("app", ""),
        arguments.get("query", ""),
    )


def _handle_create_file(arguments, user_input="", quick_chat_fn=None):
    from brain_defs import _execute_create_file
    return _execute_create_file(
        arguments.get("path", ""),
        arguments.get("content", ""),
        quick_chat_fn=quick_chat_fn,
        user_request=user_input or "",
    )


def _handle_type_text(arguments):
    from computer import type_text
    return type_text(arguments.get("text", ""))


def _handle_press_key(arguments):
    from computer import press_key
    return press_key(arguments.get("keys", ""))


def _handle_click_at(arguments):
    from computer import click_at
    x = arguments.get("x") or 0
    y = arguments.get("y") or 0
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return "Error: x and y must be numbers."
    return click_at(int(x), int(y), arguments.get("button") or "left")


def _handle_scroll(arguments):
    direction = arguments.get("direction", "down").lower()
    try:
        import pyautogui
        clicks = -3 if direction == "down" else 3
        pyautogui.scroll(clicks)
        return f"Scrolled {direction}."
    except Exception as e:
        return f"Scroll failed: {e}"


def _handle_take_screenshot(arguments):
    from vision import analyze_screen
    return analyze_screen(arguments.get("question", "What is on the screen?"))


def _handle_find_on_screen(arguments):
    element_name = arguments.get("element", "")

    # Use tiered resolver: UIA first, vision fallback
    try:
        from automation.resolve import resolve_target
        target = resolve_target(element_name, try_vision=True)
        if target.found:
            return (f"Found '{target.name}' ({target.type or target.source}) "
                    f"at ({target.x}, {target.y})")
        return f"Not found: {target.error}"
    except Exception as e:
        logger.debug(f"resolve_target failed, falling back: {e}")

    # Direct fallback if resolver module fails
    try:
        from automation.ui_control import find_control
        ctrl = find_control(name=element_name)
        if ctrl:
            return f"Found '{ctrl['name']}' ({ctrl['type']}) at ({ctrl['x']}, {ctrl['y']})"
    except Exception:
        pass

    from vision import find_element
    result = find_element(element_name)
    if result.get("found"):
        return f"Found at ({result['x']}, {result['y']}): {result.get('description', '')}"
    return f"Not found: {result.get('description', 'element not visible')}"


def _handle_click_element(arguments):
    # Use new UI control module (falls back to coordinate clicking)
    from automation.ui_control import click_control
    return click_control(name=arguments.get("name", ""))


def _handle_manage_tabs(arguments):
    from computer import manage_tabs
    return manage_tabs(
        arguments.get("action", "list"),
        arguments.get("index"),
    )


def _handle_fill_form(arguments):
    from computer import fill_form_fields
    return fill_form_fields(arguments.get("fields", {}))


def _handle_focus_window(arguments):
    from automation.ui_control import focus_window
    return focus_window(arguments.get("name", ""))


def _handle_click_control(arguments):
    from automation.ui_control import click_control
    return click_control(
        name=arguments.get("name"),
        role=arguments.get("role"),
        automation_id=arguments.get("automation_id"),
        window=arguments.get("window"),
    )


def _handle_inspect_window(arguments):
    from automation.ui_control import inspect_window
    return inspect_window(
        name=arguments.get("name"),
        max_controls=arguments.get("max_controls", 20),
    )


def _handle_set_control_text(arguments):
    from automation.ui_control import set_control_text
    return set_control_text(
        name=arguments.get("control"),
        text=arguments.get("text", ""),
        window=arguments.get("window"),
    )


def _handle_snap_window(arguments):
    from automation.window_manager import snap_window
    return snap_window(
        arguments.get("name", ""),
        arguments.get("position", "maximize"),
    )


def _handle_list_windows(arguments):
    from automation.window_manager import list_windows
    windows = list_windows()
    if not windows:
        return "No windows found."
    lines = [f"Open windows ({len(windows)}):"]
    for w in windows:
        state = " (minimized)" if w.get("minimized") else ""
        proc = w.get("process_name", "")
        proc_str = f" [{proc}]" if proc else ""
        lines.append(f"  {w['title'][:60]}{proc_str}{state}")
    return "\n".join(lines)


def _handle_agent_task(arguments, action_registry=None, reminder_mgr=None, speak_fn=None):
    """Launch autonomous desktop agent for multi-step tasks."""
    goal = arguments.get("goal", "")
    goal_lower = goal.lower()

    # Spotify music → try search_in_app first, fall through to agent if no results
    if any(w in goal_lower for w in ["spotify", "play music", "play a song", "play song"]):
        music_query = "popular hits"
        for pattern in ["play (.+?) on spotify", "play (.+?) in spotify",
                        "play (.+?) spotify", "spotify.*play (.+)",
                        "play (.+)"]:
            m = re.search(pattern, goal_lower)
            if m:
                extracted = m.group(1).strip()
                extracted = re.sub(r'^(a |an |some |the |any )', '', extracted).strip()
                if extracted and extracted not in ("music", "song", "songs"):
                    music_query = extracted
                break
        from computer import search_in_app
        result = search_in_app("Spotify", music_query)
        # If search found results, return immediately; if no results, fall through to agent
        if result and "no results" not in result.lower():
            return result
        # Fall through to full agent mode for retry with different approach

    # Bluetooth/WiFi toggle — open settings page first
    if any(w in goal_lower for w in ["bluetooth", "wifi", "wi-fi"]):
        setting = "bluetooth" if "bluetooth" in goal_lower else "wifi"
        if action_registry and "open_app" in action_registry:
            action_registry["open_app"](setting)
            import time as _time
            _time.sleep(2)

    from desktop_agent import DesktopAgent
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    agent = DesktopAgent(
        action_registry=action_registry,
        reminder_mgr=reminder_mgr,
        speak_fn=speak_fn,
    )

    # Scale timeout by model size (32b needs much more time than 7b)
    _agent_timeout = 120  # base: 2 minutes
    try:
        from config import load_config
        _cfg = load_config()
        _model = (_cfg.get("ollama_model") or "").lower()
        if any(s in _model for s in ("72b", "70b")):
            _agent_timeout = 480  # 8 min
        elif any(s in _model for s in ("32b", "27b")):
            _agent_timeout = 300  # 5 min
        elif any(s in _model for s in ("14b", "13b")):
            _agent_timeout = 180  # 3 min
    except Exception:
        pass

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent.execute, goal)
            result = future.result(timeout=_agent_timeout)
        return result or "Task completed."
    except FuturesTimeout:
        agent.cancel()
        logger.warning(f"agent_task timed out after {_agent_timeout}s: {goal[:60]}")
        return "Task took too long. Some steps may have completed."
    except Exception as e:
        logger.error(f"agent_task failed: {e}", exc_info=True)
        return f"Agent task failed: {e}"


# ===================================================================
# Verification wrappers
# ===================================================================

def _verify_play_music(arguments, result, user_input=""):
    from tools.verifier import verify_tool_completion
    return verify_tool_completion("play_music", arguments, result, user_input)


def _verify_search_in_app(arguments, result, user_input=""):
    from tools.verifier import verify_tool_completion
    return verify_tool_completion("search_in_app", arguments, result, user_input)


# ===================================================================
# Registration
# ===================================================================

def register_desktop_tools(registry: ToolRegistry):
    """Register desktop automation, media, vision, and file tools."""

    registry.register(ToolSpec(
        name="play_music",
        description=(
            "Play, pause, skip, or control music playback. Opens Spotify/music app and uses "
            "media keys. Use for 'play music', 'pause music', 'next song', 'play [song] on Spotify'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "What to do: 'play', 'pause', 'next', 'previous', 'play_query', 'volume_up', 'volume_down', 'mute'",
                    "enum": ["play", "pause", "next", "previous", "play_query",
                             "volume_up", "volume_down", "mute"],
                },
                "query": {"type": "string", "description": "Song/artist/playlist to search for (only for play_query action)"},
                "app": {"type": "string", "description": "Music app to use: 'spotify' (default), 'youtube'"},
            },
            "required": ["action"]
        },
        handler=_handle_play_music,
        requires_user_input=True,
        requires_quick_chat=True,
        verifier=_verify_play_music,
        aliases=["play", "music", "pause", "next_song", "skip",
                 "previous_song", "media", "play_song", "pause_music",
                 "volume_up", "volume_down", "mute", "louder", "quieter", "volume"],
        primary_arg="query",
        core=True,
    ))

    registry.register(ToolSpec(
        name="search_in_app",
        description=(
            "Search WITHIN an already-open app or website. Use ONLY for 'search for X on/in Y' "
            "or 'find X in Y'. NEVER use this for file creation, weather, time, news, or settings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "The app or website name, e.g. 'YouTube', 'Spotify', 'Google'"},
                "query": {"type": "string", "description": "What to search for"},
            },
            "required": ["app", "query"]
        },
        handler=_handle_search_in_app,
        verifier=_verify_search_in_app,
        aliases=["search_app", "app_search"],
        arg_aliases={"application": "app", "in": "app", "on": "app",
                     "search": "query", "q": "query", "text": "query"},
        primary_arg="query",
        core=False,  # Cloud-only: play_music handles Spotify/YouTube search internally
    ))

    registry.register(ToolSpec(
        name="create_file",
        description=(
            "Create a FILE on disk. Use for 'create/make/build a file/document/script/project'. "
            "Saves to Desktop. NEVER use search_in_app, google_search, or agent_task for file creation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Relative file path (e.g. 'calculator.py', 'my_project/main.py'). Saved under Desktop by default."},
                "content": {"type": "string", "description": "The file content to write"},
            },
            "required": ["path", "content"]
        },
        handler=_handle_create_file,
        safety="moderate",
        requires_user_input=True,
        requires_quick_chat=True,
        aliases=["create", "make_file", "write_file", "create_document",
                 "create_word_document", "create_text_document",
                 "make_document", "new_file", "save_file", "write_document"],
        arg_aliases={"filename": "path", "name": "path", "file": "path",
                     "code": "content", "text": "content", "data": "content"},
        primary_arg="path",
        core=True,
    ))

    registry.register(ToolSpec(
        name="type_text",
        description="Type text into the currently focused application. Use after opening or focusing an app.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to type"},
            },
            "required": ["text"]
        },
        handler=_handle_type_text,
        safety="moderate",
        aliases=["type", "write", "input"],
        arg_aliases={"content": "text", "string": "text", "input": "text", "message": "text"},
        primary_arg="text",
    ))

    registry.register(ToolSpec(
        name="press_key",
        description=(
            "Press a key or key combo. Examples: 'enter', 'ctrl+c', 'alt+tab', 'ctrl+shift+t'. "
            "Blocked: Ctrl+Alt+Del, Alt+F4, Win+L."
        ),
        parameters={
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "Key or combo to press, e.g. 'enter', 'ctrl+c', 'alt+tab'"},
            },
            "required": ["keys"]
        },
        handler=_handle_press_key,
        safety="moderate",
        aliases=["press", "key", "hotkey", "keyboard"],
        arg_aliases={"key": "keys", "combo": "keys", "shortcut": "keys", "hotkey": "keys"},
        primary_arg="keys",
    ))

    registry.register(ToolSpec(
        name="click_at",
        description=(
            "Click the mouse at screen coordinates. WARNING: Use click_control instead when "
            "possible — it's more reliable. Only use click_at when you have exact coordinates "
            "from find_on_screen or take_screenshot."
        ),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate on screen"},
                "y": {"type": "integer", "description": "Y coordinate on screen"},
                "button": {"type": "string", "description": "Mouse button: left, right, or middle",
                           "enum": ["left", "right", "middle"]},
            },
            "required": ["x", "y"]
        },
        handler=_handle_click_at,
        safety="moderate",
        aliases=["click", "mouse_click"],
    ))

    registry.register(ToolSpec(
        name="scroll",
        description="Scroll up or down.",
        parameters={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
            },
            "required": []
        },
        handler=_handle_scroll,
        safety="moderate",
    ))

    registry.register(ToolSpec(
        name="take_screenshot",
        description=(
            "Take a screenshot and analyze what's visible on screen. Use to see what's happening, "
            "check if an action worked, or understand the current state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "What to look for or ask about the screen, e.g. 'What apps are open?', 'Is there a dialog box?'"},
            },
            "required": ["question"]
        },
        handler=_handle_take_screenshot,
        aliases=["screenshot", "capture", "screen", "capture_screen"],
        arg_aliases={"q": "question", "prompt": "question", "ask": "question", "query": "question"},
        primary_arg="question",
    ))

    registry.register(ToolSpec(
        name="find_on_screen",
        description=(
            "Find a UI element on screen and return its coordinates. "
            "Use to locate buttons, text fields, icons before clicking."
        ),
        parameters={
            "type": "object",
            "properties": {
                "element": {"type": "string",
                            "description": "What to find, e.g. 'the Start menu button', 'the search bar', 'the OK button'"},
            },
            "required": ["element"]
        },
        handler=_handle_find_on_screen,
        aliases=["find", "locate", "find_element"],
        arg_aliases={"target": "element", "item": "element", "object": "element",
                     "name": "element", "what": "element"},
        primary_arg="element",
    ))

    registry.register(ToolSpec(
        name="click_element",
        description="Click a UI element by name via accessibility tree.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Element name to click"},
            },
            "required": ["name"]
        },
        handler=_handle_click_element,
        safety="moderate",
        aliases=["click_button"],
    ))

    registry.register(ToolSpec(
        name="manage_tabs",
        description="Manage browser tabs (list, switch, close).",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "switch", "close", "new"]},
                "index": {"type": "integer", "description": "Tab index"},
            },
            "required": ["action"]
        },
        handler=_handle_manage_tabs,
        safety="moderate",
        aliases=["tab", "tabs", "new_tab", "close_tab", "switch_tab"],
    ))

    registry.register(ToolSpec(
        name="fill_form",
        description="Fill form fields in the current application.",
        parameters={
            "type": "object",
            "properties": {
                "fields": {"type": "object", "description": "Field name → value mapping"},
            },
            "required": ["fields"]
        },
        handler=_handle_fill_form,
        safety="moderate",
        aliases=["form", "fill"],
    ))

    registry.register(ToolSpec(
        name="agent_task",
        description=(
            "Launch an autonomous AI agent to complete a multi-step task on screen. "
            "The agent observes the screen, plans steps, and executes them. Use for compound "
            "commands like 'open Spotify and play jazz', 'open Chrome and go to github.com', "
            "'search for something on YouTube'. The agent handles all screen interactions "
            "(clicking, typing, navigating). DO NOT use for simple single-action tasks "
            "that have dedicated tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The task to accomplish, e.g. 'search for ACDC on YouTube', 'open Spotify and play jazz'"},
            },
            "required": ["goal"]
        },
        handler=_handle_agent_task,
        safety="critical",
        requires_registry=True,
        requires_reminder_mgr=True,
        requires_speak_fn=True,
        aliases=["desktop", "task"],
        primary_arg="goal",
    ))

    # --- Phase 16: Semantic UI automation tools ---

    registry.register(ToolSpec(
        name="focus_window",
        description=(
            "Switch to a window by name. Brings it to the foreground, restores if minimized. "
            "Use for: 'switch to Chrome', 'go to Discord', 'show File Explorer', "
            "'bring up Settings'. Prefer this over open_app when the app is already running."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Window title or app name (e.g. 'Chrome', 'Settings', 'File Explorer')"},
            },
            "required": ["name"]
        },
        handler=_handle_focus_window,
        aliases=["focus", "switch_to", "switch", "focus_app", "activate",
                 "show_window", "bring_up", "go_to", "switch_window"],
        arg_aliases={"app": "name", "window": "name", "title": "name",
                     "application": "name", "target": "name"},
        primary_arg="name",
        core=False,  # Cloud-only: open_app already focuses running apps
    ))

    registry.register(ToolSpec(
        name="click_control",
        description=(
            "Click a UI control (button, link, menu item, checkbox) by its name via the "
            "accessibility tree. More reliable than coordinate clicking. Use for: 'click the "
            "Submit button', 'click Sign in', 'click the File menu'. Prefer this over click_at."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Control text/label (e.g. 'Submit', 'Sign in', 'File', 'Close')"},
                "role": {"type": "string",
                         "description": "Control type: Button, Hyperlink, MenuItem, CheckBox, TabItem, ComboBox"},
                "window": {"type": "string",
                           "description": "Window to search in (default: active window)"},
            },
            "required": ["name"]
        },
        handler=_handle_click_control,
        safety="moderate",
        aliases=["press_button", "invoke"],
        arg_aliases={"label": "name", "text": "name", "element": "name",
                     "button": "name", "control": "name", "type": "role"},
        primary_arg="name",
    ))

    registry.register(ToolSpec(
        name="inspect_window",
        description=(
            "See what controls (buttons, inputs, menus) are in a window without taking a "
            "screenshot. Faster and more reliable than vision. Use to understand a window's "
            "layout before interacting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Window title (default: active window)"},
            },
            "required": []
        },
        handler=_handle_inspect_window,
        aliases=["inspect", "window_info", "show_controls", "get_controls"],
        arg_aliases={"window": "name", "title": "name", "app": "name"},
        primary_arg="name",
    ))

    registry.register(ToolSpec(
        name="set_control_text",
        description=(
            "Set text on an input field found via accessibility tree. "
            "More reliable than type_text — targets specific controls."
        ),
        parameters={
            "type": "object",
            "properties": {
                "control": {"type": "string",
                            "description": "Input field name/label"},
                "text": {"type": "string",
                         "description": "Text to set"},
                "window": {"type": "string",
                           "description": "Window to search in"},
            },
            "required": ["control", "text"]
        },
        handler=_handle_set_control_text,
        safety="moderate",
        aliases=["set_text", "fill_field"],
        arg_aliases={"field": "control", "input": "control", "name": "control",
                     "value": "text", "content": "text"},
        primary_arg="control",
    ))

    registry.register(ToolSpec(
        name="snap_window",
        description=(
            "Snap a window to a screen position: left half, right half, maximize, minimize, "
            "center. Use for: 'snap Chrome to the left', 'maximize Firefox', "
            "'put Discord on the right side'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Window title or app name"},
                "position": {"type": "string",
                             "description": "Where to put the window",
                             "enum": ["left", "right", "maximize", "minimize",
                                      "center", "restore",
                                      "top-left", "top-right",
                                      "bottom-left", "bottom-right"]},
            },
            "required": ["name", "position"]
        },
        handler=_handle_snap_window,
        aliases=["snap", "arrange", "dock"],
        arg_aliases={"app": "name", "window": "name", "title": "name",
                     "side": "position", "direction": "position"},
        primary_arg="name",
    ))

    registry.register(ToolSpec(
        name="list_windows",
        description="List all open windows with titles and process names.",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_handle_list_windows,
        aliases=["show_windows", "open_windows", "windows"],
    ))

    # browser_action is now registered by tools/browser_tools.py (CDP persistent session)

    logger.info("Registered 19 desktop tools")
