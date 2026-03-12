"""
Built-in tool registrations for the first 5 migrated tools.

Registers: open_app, google_search, get_weather, set_reminder, send_email
into the ToolRegistry with full ToolSpec metadata.

Each tool has:
  - Handler function (extracted from brain.py _execute_tool_inner)
  - OpenAI-format parameter schema
  - Safety/confirmation config
  - Verification support (where applicable)
  - Undo/rollback support (where applicable)
  - Cache config (where applicable)
"""

import logging
import os
import subprocess

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ===================================================================
# Handler functions
# ===================================================================

def _handle_open_app(arguments, action_registry=None):
    """Open an application by name, with pronoun and category resolution."""
    name = arguments.get("name", "")
    if not isinstance(name, str):
        name = str(name) if name else ""
    name = name.strip()
    if not name:
        return "Error: no app name provided."

    # Pronoun resolution: "open it" → open last created file
    if name.lower() in ("it", "this", "that", "the file", "the result"):
        try:
            from brain import _brain_state
            if _brain_state.last_created_file and os.path.exists(_brain_state.last_created_file):
                subprocess.Popen(["start", "", _brain_state.last_created_file], shell=True)
                return f"Opening {os.path.basename(_brain_state.last_created_file)}"
        except Exception:
            pass

    # App category resolution: "browser" → user's preferred browser
    try:
        from memory import UserPreferences, MemoryStore
        _prefs = UserPreferences(MemoryStore())
        resolved = _prefs.resolve_app_category(name)
        if resolved.lower() != name.lower():
            logger.info(f"App category '{name}' → '{resolved}'")
            name = resolved
    except Exception:
        pass

    if not action_registry or "open_app" not in action_registry:
        return f"Error: open_app not available in action registry."
    return action_registry["open_app"](name)


def _handle_google_search(arguments, action_registry=None):
    """Search Google for a query."""
    if not action_registry or "google_search" not in action_registry:
        return "Error: google_search not available in action registry."
    return action_registry["google_search"](arguments.get("query", ""))


def _handle_get_weather(arguments):
    """Get current weather or forecast, optionally for a specific city."""
    city = arguments.get("city", "") or None
    # Check if the user is asking for a forecast (tomorrow, this week, etc.)
    # The LLM sometimes sends forecast requests to get_weather since get_forecast
    # isn't always available as a core tool for Ollama
    try:
        from brain import execute_tool
        user_input = getattr(execute_tool, '_last_user_input', '') or ''
    except Exception:
        user_input = ''
    _forecast_words = ("tomorrow", "forecast", "next week", "this week", "weekend",
                       "will it rain", "will it snow", "next few days")
    if any(w in user_input.lower() for w in _forecast_words):
        try:
            from weather import get_forecast
            return get_forecast(city)
        except Exception:
            pass
    from weather import get_current_weather
    return get_current_weather(city)


def _handle_set_reminder(arguments, action_registry=None, reminder_mgr=None):
    """Set a reminder with message and time."""
    msg = arguments.get("message", "")
    t = arguments.get("time", "in 1 hour")
    # Try direct reminder_mgr first (more reliable)
    if reminder_mgr:
        return reminder_mgr.add_reminder(msg, t)
    if action_registry and "set_reminder" in action_registry:
        return action_registry["set_reminder"](f"{msg}|{t}")
    # Last resort: try importing
    try:
        from reminders import ReminderManager
        rm = ReminderManager()
        return rm.add_reminder(msg, t)
    except Exception as e:
        return f"Error setting reminder: {e}"


def _handle_send_email(arguments):
    """Send an email via SMTP."""
    from email_sender import send_email
    return send_email(
        arguments.get("to", ""),
        arguments.get("subject", ""),
        arguments.get("body", ""),
    )


def _handle_search_skills(arguments=None, **kwargs):
    """Search the skill library by keyword or category."""
    if not arguments:
        return "No search criteria provided"

    query = arguments.get("query", "")
    category = arguments.get("category", "")

    try:
        from skills import SkillLibrary
        sl = SkillLibrary()

        if category:
            results = sl.find_by_category(category, limit=5)
        elif query:
            results = sl.find_skill(query, min_similarity=0.3, limit=5)
        else:
            return "Provide a query or category to search"

        if not results:
            return f"No skills found for '{query or category}'"

        lines = []
        for r in results:
            cat = r.get("category", "general")
            lines.append(f"- {r['name']} [{cat}]: {r.get('goal', r.get('description', ''))[:60]}")
        return f"Found {len(results)} skills:\n" + "\n".join(lines)
    except Exception as e:
        return f"Skill search error: {e}"


def _handle_search_tools(arguments):
    """Search available tools by keyword. Returns matching tool names and descriptions."""
    query = arguments.get("query", "").lower().strip()
    if not query:
        return "Error: provide a search query, e.g. 'file management' or 'screenshot'"

    from tools.registry import get_default
    reg = get_default()
    if not reg:
        return "Tool registry not available."

    keywords = query.split()
    matches = []
    for spec in reg.all_specs():
        if not spec.llm_enabled:
            continue
        # Score: check name, description, aliases
        searchable = f"{spec.name} {spec.description} {' '.join(spec.aliases)}".lower()
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            matches.append((score, spec.name, spec.description[:80]))

    matches.sort(key=lambda x: -x[0])
    if not matches:
        return f"No tools found matching '{query}'. Try broader keywords."

    lines = []
    for score, name, desc in matches[:8]:
        lines.append(f"- {name}: {desc}")
    return f"Found {len(matches)} tools:\n" + "\n".join(lines)


# ===================================================================
# Rollback functions
# ===================================================================

def _rollback_open_app(arguments, action_registry):
    """Undo open_app by closing the app."""
    name = arguments.get("name", "")
    close_fn = action_registry.get("close_app")
    if close_fn:
        return close_fn(name)
    return f"No close handler for {name}"


# ===================================================================
# Verification functions (for open_app and google_search)
# ===================================================================
# These delegate to tools.verifier which already has the full logic.
# The ToolSpec.verifier is called by the executor's post-execution hooks.

def _verify_open_app(arguments, result, user_input=""):
    """Verify open_app completed."""
    from tools.verifier import verify_tool_completion
    return verify_tool_completion("open_app", arguments, result, user_input)


def _verify_google_search(arguments, result, user_input=""):
    """Verify google_search completed."""
    from tools.verifier import verify_tool_completion
    return verify_tool_completion("google_search", arguments, result, user_input)


# ===================================================================
# Registration
# ===================================================================

def register_builtin_tools(registry: ToolRegistry):
    """Register the first 5 migrated tools into the registry."""

    registry.register(ToolSpec(
        name="open_app",
        description=(
            "Open any application by name. Use for: 'open Chrome', "
            "'launch Spotify', 'start Notepad'. Always use this for opening apps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Application name to open, e.g. 'Chrome', 'Spotify', 'Settings'"
                }
            },
            "required": ["name"]
        },
        handler=_handle_open_app,
        requires_registry=True,
        verifier=_verify_open_app,
        rollback=_rollback_open_app,
        rollback_description="opened {name}",
        aliases=["open", "launch", "start", "open_application", "launch_app", "run_app"],
        arg_aliases={"app_name": "name", "app": "name", "application": "name"},
        primary_arg="name",
        core=True,
    ))

    registry.register(ToolSpec(
        name="google_search",
        description=(
            "Search the web using Google. Opens results in the browser. "
            "Use for 'search for X', 'google Y'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for"
                }
            },
            "required": ["query"]
        },
        handler=_handle_google_search,
        requires_registry=True,
        verifier=None,  # No verification — webbrowser.open() is instant and reliable
        aliases=["search", "web_search", "google", "browse", "search_web"],
        arg_aliases={"q": "query", "search": "query", "search_query": "query", "text": "query"},
        primary_arg="query",
        core=True,
    ))

    registry.register(ToolSpec(
        name="get_weather",
        description=(
            "Get current weather conditions. Use for 'what's the weather', "
            "'is it raining', 'temperature'. ALWAYS use this for weather "
            "— never answer weather from memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional, defaults to current location)"
                }
            },
            "required": []
        },
        handler=_handle_get_weather,
        cacheable=True,
        cache_ttl=300,
        aliases=["check_weather", "weather", "find_weather", "current_weather"],
        arg_aliases={"location": "city", "place": "city", "area": "city", "where": "city"},
        primary_arg="city",
        core=True,
    ))

    registry.register(ToolSpec(
        name="set_reminder",
        description=(
            "Set a timed reminder. Use for 'remind me to X at Y', "
            "'set a reminder for X'. ALWAYS use this instead of create_file for reminders."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to remind the user about"
                },
                "time": {
                    "type": "string",
                    "description": "When to remind, e.g. 'in 30 minutes', '5pm', 'tomorrow at 9am'"
                }
            },
            "required": ["message", "time"]
        },
        handler=_handle_set_reminder,
        requires_registry=True,
        requires_reminder_mgr=True,
        aliases=["add_reminder", "create_reminder", "reminder"],
        arg_aliases={"reminder": "message", "text": "message", "what": "message",
                     "description": "message", "content": "message", "note": "message",
                     "when": "time", "at": "time", "datetime": "time"},
        primary_arg="message",
        core=True,
    ))

    registry.register(ToolSpec(
        name="send_email",
        description=(
            "Send an actual email via SMTP. Use this for 'send email to X', "
            "'email X about Y'. ALWAYS use this for email — do NOT use create_file for emails."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address"
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line"
                },
                "body": {
                    "type": "string",
                    "description": "Email body text"
                }
            },
            "required": ["to", "subject", "body"]
        },
        handler=_handle_send_email,
        safety="sensitive",
        confirm_condition=lambda args: f"send an email to {args.get('to', 'someone')} about {args.get('subject', 'something')}",
        aliases=["send_message", "compose_email", "email", "mail"],
        primary_arg="to",
        core=False,  # Cloud-only: rarely used, complex params confuse 7B model
    ))

    registry.register(ToolSpec(
        name="search_tools",
        description=(
            "Search for available tools by keyword. Use when you need to find "
            "the right tool for a task. Returns tool names and descriptions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for, e.g. 'file', 'screenshot', 'music', 'terminal'"
                }
            },
            "required": ["query"]
        },
        handler=_handle_search_tools,
        aliases=["find_tool", "list_tools", "available_tools", "tool_search"],
        primary_arg="query",
        core=False,  # Cloud-only: meta tool confuses 7B model
    ))

    registry.register(ToolSpec(
        name="search_skills",
        description=(
            "Search the skill library for reusable learned skills by keyword or category. "
            "Categories: web, system, communication, automation, general."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for, e.g. 'weather', 'open browser', 'send email'"
                },
                "category": {
                    "type": "string",
                    "description": "Category filter: web, system, communication, automation, general"
                }
            },
            "required": []
        },
        handler=_handle_search_skills,
        aliases=["find_skill", "list_skills", "skill_search"],
        primary_arg="query",
        core=False,
    ))

    logger.info(f"Registered {len(registry.all_names())} built-in tools: {registry.all_names()}")
