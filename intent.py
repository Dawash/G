"""
Intent detection — determines what the user wants to do.

Three-tier strategy:
  1. AI-based classification using the user's configured LLM provider
     (returns structured JSON with intents + entities).
  2. Enhanced keyword matching (fast, offline fallback).
  3. Default to chat if nothing matches.

The AI tier handles complex multi-step commands like
"open Chrome and search for weather" by returning a list of actions.
"""

import re
import json
import time
import logging
import requests

# ---------------------------------------------------------------------------
# Intent types
# ---------------------------------------------------------------------------
INTENT_QUIT = "quit"
INTENT_DISCONNECT = "disconnect"
INTENT_CONNECT = "connect"
INTENT_SHUTDOWN = "shutdown"
INTENT_RESTART = "restart"
INTENT_CANCEL_SHUTDOWN = "cancel_shutdown"
INTENT_SLEEP = "sleep"
INTENT_GOOGLE_SEARCH = "google_search"
INTENT_OPEN_APP = "open_app"
INTENT_CLOSE_APP = "close_app"
INTENT_MINIMIZE_APP = "minimize_app"
INTENT_WEATHER = "weather"
INTENT_FORECAST = "forecast"
INTENT_TIME = "time"
INTENT_NEWS = "news"
INTENT_SET_REMINDER = "set_reminder"
INTENT_LIST_REMINDERS = "list_reminders"
INTENT_SNOOZE = "snooze"
INTENT_SWITCH_PROVIDER = "switch_provider"
INTENT_CHAT = "chat"

# All valid intent labels the AI classifier is allowed to return.
VALID_INTENTS = {
    INTENT_QUIT,
    INTENT_DISCONNECT,
    INTENT_CONNECT,
    INTENT_SHUTDOWN,
    INTENT_RESTART,
    INTENT_CANCEL_SHUTDOWN,
    INTENT_SLEEP,
    INTENT_GOOGLE_SEARCH,
    INTENT_OPEN_APP,
    INTENT_CLOSE_APP,
    INTENT_MINIMIZE_APP,
    INTENT_WEATHER,
    INTENT_FORECAST,
    INTENT_TIME,
    INTENT_NEWS,
    INTENT_SET_REMINDER,
    INTENT_LIST_REMINDERS,
    INTENT_SNOOZE,
    INTENT_SWITCH_PROVIDER,
    INTENT_CHAT,
}


# ===================================================================
# AI-based intent classification
# ===================================================================

# The prompt is the single most important piece — it must be tight,
# unambiguous, and produce valid JSON on every model we support.
INTENT_SYSTEM_PROMPT = """\
You are an intent classifier for a Windows voice assistant.

Given the user's spoken command, return a JSON object with a single key
"actions" whose value is an array of action objects.

Each action object has exactly two keys:
  "intent" — one of the labels listed below
  "entity" — the relevant extracted value (string), or null if not needed

### Intent labels (use ONLY these)
  quit            — user wants to exit/stop the assistant
  disconnect      — user wants to go offline
  connect         — user wants to reconnect
  shutdown        — shut down the computer
  restart         — restart / reboot the computer
  cancel_shutdown — cancel a pending shutdown or restart
  sleep           — put the computer to sleep
  google_search   — web search  (entity = the search query)
  open_app        — open/launch an application  (entity = app name)
  close_app       — close an application  (entity = app name)
  minimize_app    — minimize an application  (entity = app name)
  weather         — get the current weather
  forecast        — get weather forecast for coming hours
  time            — ask for the current time
  news            — get news headlines  (entity = category: general/tech/sports/entertainment/science or null)
  set_reminder    — set a reminder  (entity = "message|time" e.g. "call John|5pm" or "meeting|in 30 minutes")
  list_reminders  — show active reminders
  snooze          — snooze the last reminder  (entity = minutes or null for default 10)
  chat            — general conversation / question  (entity = the full message)

### Rules
1. If the command contains MULTIPLE actions, return them all in order.
   Example: "open Chrome and search for weather" →
     [{"intent":"open_app","entity":"chrome"},{"intent":"google_search","entity":"weather"}]
2. Extract entities cleanly — strip filler words ("please", "can you").
3. For google_search, entity is the search query only, not "search for X".
4. For chat, set entity to the user's full message.
5. Return ONLY the JSON object. No markdown, no explanation.\
"""

# Timeout for the classification call — must be fast for voice UX.
_CLASSIFY_TIMEOUT = 6  # seconds


def _build_openai_payload(user_text, model):
    """Build request body for OpenAI / OpenRouter (same format)."""
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
    }


def _build_anthropic_payload(user_text, model):
    """Build request body for Anthropic Messages API."""
    return {
        "model": model,
        "max_tokens": 300,
        "temperature": 0,
        "system": INTENT_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_text},
        ],
    }


def _extract_content_openai(data):
    """Pull the assistant message text from an OpenAI-shaped response."""
    return data["choices"][0]["message"]["content"]


def _extract_content_anthropic(data):
    """Pull the assistant message text from an Anthropic response."""
    return data["content"][0]["text"]


def _get_ollama_base_url():
    """Get the Ollama base URL from config, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_URL
        cfg = load_config()
        return cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    except Exception:
        return "http://localhost:11434"


def _build_ollama_payload(user_text, model):
    """Build request body for native Ollama /api/chat endpoint."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 300},
    }


def _extract_content_ollama(data):
    """Pull the assistant message text from a native Ollama response."""
    return data["message"]["content"]


# Provider-specific configuration looked up by name.
_PROVIDER_CONFIG = {
    "ollama": {
        "url": None,  # Resolved dynamically from config via _get_ollama_base_url()
        "model": "qwen2.5:7b",
        "build_payload": _build_ollama_payload,
        "extract": _extract_content_ollama,
        "headers": lambda key: {
            "Content-Type": "application/json",
        },
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "build_payload": _build_openai_payload,
        "extract": _extract_content_openai,
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "gpt-4o-mini",
        "build_payload": _build_openai_payload,
        "extract": _extract_content_openai,
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "build_payload": _build_anthropic_payload,
        "extract": _extract_content_anthropic,
        "headers": lambda key: {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    },
}


def _parse_actions(raw_json_text):
    """
    Parse the LLM's JSON response into a list of (intent, entity) tuples.

    Returns a list even for single-action commands so the caller can
    always iterate.  Invalid / unrecognised items are silently dropped.
    """
    # Strip markdown fences if the model wraps them anyway.
    text = raw_json_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    data = json.loads(text)

    actions_raw = data.get("actions", [])
    if not isinstance(actions_raw, list):
        actions_raw = [actions_raw]

    result = []
    for item in actions_raw:
        intent = item.get("intent", "").lower().strip()
        entity = item.get("entity")
        if intent not in VALID_INTENTS:
            logging.warning("AI returned unknown intent %r — skipping", intent)
            continue
        result.append((intent, entity))

    return result


def classify_with_ai(user_text, provider_name, api_key):
    """
    Send user_text to the configured LLM and return a list of
    (intent, entity) tuples.

    Returns None on any failure so the caller can fall back to keywords.
    """
    # Skip if rate-limited (avoid wasting time on a call that will 429)
    try:
        from ai_providers import is_rate_limited
        if is_rate_limited():
            logging.info("Skipping AI intent classification — rate limited")
            return None
    except ImportError:
        pass

    cfg = _PROVIDER_CONFIG.get(provider_name)
    if cfg is None:
        logging.error("No AI intent config for provider %r", provider_name)
        return None

    # Resolve Ollama URL and model dynamically from config
    url = cfg["url"]
    model = cfg["model"]
    if provider_name == "ollama":
        if url is None:
            url = f"{_get_ollama_base_url()}/api/chat"
        try:
            from config import load_config, DEFAULT_OLLAMA_MODEL
            model = load_config().get("ollama_model", DEFAULT_OLLAMA_MODEL)
        except Exception:
            pass  # keep static default

    payload = cfg["build_payload"](user_text, model)
    headers = cfg["headers"](api_key)

    try:
        t0 = time.perf_counter()
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=_CLASSIFY_TIMEOUT,
        )
        resp.raise_for_status()
        elapsed = time.perf_counter() - t0
        logging.info("AI intent classification took %.2fs", elapsed)

        # Clear rate limit on success
        try:
            from ai_providers import _clear_rate_limit
            _clear_rate_limit()
        except ImportError:
            pass

        content = cfg["extract"](resp.json())
        actions = _parse_actions(content)
        if actions:
            return actions
        logging.warning("AI returned empty actions list — falling back")
        return None

    except requests.Timeout:
        logging.warning("AI intent classification timed out (%.1fs limit)",
                        _CLASSIFY_TIMEOUT)
        return None
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            try:
                from ai_providers import _record_rate_limit
                _record_rate_limit()
            except ImportError:
                pass
        logging.warning("AI intent classification request failed: %s", exc)
        return None
    except requests.RequestException as exc:
        logging.warning("AI intent classification request failed: %s", exc)
        return None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logging.warning("Failed to parse AI intent response: %s", exc)
        return None


# ===================================================================
# Keyword-based fallback (fast, fully offline)
# ===================================================================

def _keyword_detect(user_input):
    """
    Original keyword-matching logic.
    Returns (intent_type, extracted_data) tuple.
    """
    text = user_input.lower().strip()

    # Exit commands
    if text in ("quit", "exit", "bye", "see ya", "goodbye", "stop",
                "good night", "goodnight", "turn off", "i'm done", "im done"):
        return INTENT_QUIT, None
    if re.search(r"\bsee you\b", text):
        return INTENT_QUIT, None

    # Connection control
    if text == "disconnect":
        return INTENT_DISCONNECT, None
    if "connect me" in text:
        return INTENT_CONNECT, None

    # System commands (check before generic "open"/"close")
    # Note: "shut down <app>" is close_app, not shutdown. Check for a trailing
    # word that is NOT a system keyword (computer, pc, system, my computer).
    if "cancel shutdown" in text or "cancel restart" in text:
        return INTENT_CANCEL_SHUTDOWN, None
    _shutdown_system_words = {"computer", "pc", "system", "my computer", "the computer", "this computer", ""}
    shut_match = re.search(r"\b(?:shut\s*down|shutdown)\s*(.*?)$", text)
    if shut_match:
        shut_target = shut_match.group(1).strip().rstrip(".")
        if shut_target in _shutdown_system_words:
            return INTENT_SHUTDOWN, None
        else:
            # "shut down spotify" → close_app
            return INTENT_CLOSE_APP, shut_target
    if "restart" in text or "reboot" in text:
        return INTENT_RESTART, None
    if text == "sleep" or "go to sleep" in text or "sleep mode" in text:
        return INTENT_SLEEP, None

    # Reminders (including alarm/alert synonyms)
    if re.search(r"\bremind me\b|\bset a reminder\b|\breminder\b|\balert me\b|\bset (?:an? )?alarm\b|\balarm for\b", text):
        # Extract "remind me to X at Y" pattern
        match = re.search(r"remind me (?:to )?(.+?)(?:\s+(?:at|in|on|every)\s+(.+))?$", text)
        if match:
            message = match.group(1).strip()
            time_part = match.group(2) or ""
            return INTENT_SET_REMINDER, f"{message}|{time_part}".strip("|")
        # "alert me in 10 minutes" pattern
        alert_match = re.search(r"alert me\s+(.+?)$", text)
        if alert_match:
            return INTENT_SET_REMINDER, alert_match.group(1).strip()
        # "set alarm for 7am" / "alarm for 6:30" pattern
        alarm_match = re.search(r"alarm\s+(?:for\s+)?(.+?)$", text)
        if alarm_match:
            return INTENT_SET_REMINDER, alarm_match.group(1).strip()
        return INTENT_SET_REMINDER, text.replace("remind me", "").replace("set a reminder", "").strip()

    if re.search(r"\bmy reminders\b|\blist reminders\b|\bshow reminders\b|\bwhat reminders\b", text):
        return INTENT_LIST_REMINDERS, None

    if "snooze" in text:
        match = re.search(r"snooze\s*(\d+)?", text)
        minutes = match.group(1) if match and match.group(1) else None
        return INTENT_SNOOZE, minutes

    # Provider switching
    provider_match = re.search(r"\b(?:switch to|use|change to)\s+(ollama|openai|anthropic|openrouter)\b", text)
    if provider_match:
        return INTENT_SWITCH_PROVIDER, provider_match.group(1).lower()

    # News
    if re.search(r"\bnews\b|\bheadlines\b|\bbriefing\b|\bcurrent events\b|\bwhat happened today\b|\bwhat.s happening\b", text):
        category = "general"
        for cat in ("tech", "sports", "entertainment", "science", "business", "health"):
            if cat in text:
                category = cat
                break
        return INTENT_NEWS, category

    # Weather & Forecast
    # Forecast: future-oriented patterns (tomorrow, weekend, this week, "will it rain/snow")
    if re.search(r"\bforecast\b|\b(?:tomorrow|weekend|next week|this week)\b.*\b(?:weather|rain|snow|cold|hot|warm)\b|\b(?:weather|rain|snow|cold|hot|warm)\b.*\b(?:tomorrow|weekend|next week|this week)\b", text):
        return INTENT_FORECAST, None
    if re.search(r"\bwill it (?:rain|snow)\b", text):
        # "will it rain today" → weather, "will it rain tomorrow" → forecast
        if re.search(r"\b(?:tomorrow|weekend|next week|this week)\b", text):
            return INTENT_FORECAST, None
        return INTENT_WEATHER, None
    if re.search(r"\bweather\b|\bis it raining\b|\btemperature\s*(?:outside)?\b|\bhow (?:hot|cold|warm) is it\b|\bis it (?:cold|hot|warm|raining)\s*(?:outside)?\b", text):
        return INTENT_WEATHER, None

    # Time / Date
    if re.search(r"\bwhat time\b|\bwhat.s the time\b|\btell me the time\b|\bcurrent time\b|\btime please\b", text):
        return INTENT_TIME, None
    if re.search(r"\bwhat day is it\b|\bwhat.s today.s date\b|\bwhat is the date\b|\btoday.s date\b|\bwhat date is it\b", text):
        return INTENT_TIME, None

    # App control — check BEFORE search so "open X and search Y" works
    open_match = re.search(r"\b(?:open|launch|start|run|fire up)\s+(.+?)(?:\s+(?:and|then)\s+|\s+for me\s*$|\s*$)", text)
    if open_match:
        app = open_match.group(1).strip()
        for filler in ("please", "the", "app", "application", "program"):
            app = re.sub(r'\b' + filler + r'\b', '', app).strip()
        if app:
            return INTENT_OPEN_APP, app
    # "i need notepad", "get me spotify" — softer open synonyms
    soft_open_match = re.search(r"\b(?:i need|get me)\s+(.+?)$", text)
    if soft_open_match:
        app = soft_open_match.group(1).strip()
        for filler in ("please", "the", "app", "application", "program"):
            app = re.sub(r'\b' + filler + r'\b', '', app).strip()
        if app:
            return INTENT_OPEN_APP, app

    # Search
    search_match = re.search(r"\b(?:search for|google|look up)\s+(.+?)$", text)
    if search_match:
        return INTENT_GOOGLE_SEARCH, search_match.group(1).strip()

    close_match = re.search(r"\b(?:close|kill|end|exit|quit|terminate|shut)\s+(.+?)(?:\s+for me)?$", text)
    if close_match:
        app = close_match.group(1).strip()
        if app:
            return INTENT_CLOSE_APP, app

    minimize_match = re.search(r"\b(?:minimize|hide)\s+(.+?)(?:\s+for me)?$", text)
    if minimize_match:
        app = minimize_match.group(1).strip()
        if app:
            return INTENT_MINIMIZE_APP, app

    # Default: send to AI for a conversational response
    return INTENT_CHAT, user_input


# ===================================================================
# Public API
# ===================================================================

def detect_intent(user_input, provider_name=None, api_key=None,
                  use_ai=True):
    """
    Detect user intent from text input.

    Strategy:
      1. If use_ai is True and credentials are available, ask the LLM.
         This handles complex/multi-step commands and returns a *list*
         of (intent, entity) tuples.
      2. On AI failure (timeout, error, offline), fall back to keyword
         matching which returns a single (intent, entity) — wrapped in
         a list for consistency.

    Returns:
        list[tuple[str, str | None]]
        Always a list of (intent, entity) pairs, length >= 1.
    """
    # --- Try AI classification first ---
    if use_ai and provider_name and api_key:
        actions = classify_with_ai(user_input, provider_name, api_key)
        if actions is not None:
            return actions

    # --- Keyword fallback ---
    # Try to split on "and" for multi-action commands
    # e.g., "open YouTube and search for music" -> two actions
    if " and " in user_input.lower():
        parts = re.split(r"\s+and\s+", user_input, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            intent1, data1 = _keyword_detect(parts[0].strip())
            intent2, data2 = _keyword_detect(parts[1].strip())
            # Only split if both parts are real actions (not both chat)
            if intent1 != INTENT_CHAT or intent2 != INTENT_CHAT:
                actions = []
                if intent1 != INTENT_CHAT:
                    actions.append((intent1, data1))
                if intent2 != INTENT_CHAT:
                    actions.append((intent2, data2))
                if actions:
                    return actions

    intent, data = _keyword_detect(user_input)
    return [(intent, data)]
