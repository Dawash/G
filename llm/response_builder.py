"""
LLM response post-processing and cleanup.

Extracted from: brain.py Brain._sanitize_response(), Brain._is_llm_refusal(),
                Brain._suggest_tool_for_retry()

Responsibility:
  - Sanitize LLM output (strip special tokens, code fences, markdown artifacts)
  - Detect LLM refusal patterns ("I'm an AI, I can't...")
  - Suggest correct tool when LLM refuses to use tools
"""

import re
import logging

logger = logging.getLogger(__name__)


def sanitize_response(text):
    """Remove LLM artifacts (special tokens, leftover JSON) from spoken text."""
    if not text:
        return text
    # Remove llama special tokens
    text = re.sub(r'<\|.*?\|>', '', text)
    # Remove markdown code fences that leaked
    text = re.sub(r'```\w*\s*', '', text)
    # Remove qwen bracket pattern: [question → answer] or [query: answer]
    m = re.match(r'^\[.+?[→:]\s*(.+)\]$', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Remove stray leading/trailing brackets
    if text.startswith('[') and text.endswith(']') and text.count('[') == 1:
        text = text[1:-1].strip()
    # Remove markdown bold/italic that leaked into speech
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    # Remove LaTeX delimiters \[ ... \] and \( ... \)
    text = re.sub(r'\\\[(.+?)\\\]', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.+?)\\\)', r'\1', text, flags=re.DOTALL)
    # Remove \text{...} LaTeX commands
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    # Remove remaining LaTeX backslash commands (\times, \approx, etc.)
    text = re.sub(r'\\(times|approx|cdot|div|frac|sqrt|pm|mp|leq|geq|neq)', lambda m: {
        'times': '×', 'approx': '≈', 'cdot': '·', 'div': '÷',
        'sqrt': '√', 'pm': '±', 'leq': '≤', 'geq': '≥', 'neq': '≠',
    }.get(m.group(1), m.group(1)), text)
    return text.strip()


# Phrases that indicate an LLM refusing to use tools
_REFUSAL_PHRASES = [
    "i'm a large language model",
    "i don't have the ability",
    "i don't have direct access",
    "i don't have real-time",
    "i cannot directly",
    "i'm not able to",
    "i'm not currently able",
    "i am not able to",
    "as an ai language model",
    "as a text-based ai",
    "i'm a text-based",
    "text-based ai",
    "i'm an ai assistant and don't",
    "i can't directly",
    "i do not have access",
    "i don't have access to",
    "unfortunately, i'm",
    "i'm unable to",
    "i'm not actually capable",
    "i'm not capable",
    "i can't interact with",
    "i cannot interact with",
    "i don't have the capability",
    "you can follow these steps",
    "here are the steps",
    "here's how you can",
    "here are some tips",
    "tips to help you",
    "here's a step-by-step",
]


def is_llm_refusal(text):
    """Detect if LLM output is a refusal to use tools."""
    lower = text.lower()
    return any(phrase in lower for phrase in _REFUSAL_PHRASES)


def suggest_tool_for_retry(user_msg):
    """Suggest the right tool based on keywords in the user's request.

    Used when the LLM refuses to use tools — provides a hint for the retry prompt.
    """
    lower = user_msg.lower()

    _SUGGESTIONS = [
        (["weather", "temperature", "how hot", "how cold"], "Use get_weather for this. "),
        (["forecast", "will it rain", "weather tomorrow"], "Use get_forecast for this. "),
        (["time", "date", "what time", "what day"], "Use get_time for this. "),
        (["news", "headlines"], "Use get_news for this. "),
        (["play music", "play song", "play some", "play jazz", "play classical"], "Use play_music for this. "),
        (["on spotify", "on youtube"], "Use search_in_app for this. "),
    ]

    for keywords, suggestion in _SUGGESTIONS:
        if any(w in lower for w in keywords):
            return suggestion

    if "search for" in lower:
        return "Use google_search for this. "
    if any(w in lower for w in ["bluetooth", "wifi", "dark mode", "night light", "airplane"]):
        return "Use toggle_setting for this. "
    if any(w in lower for w in ["remind", "reminder", "alarm"]):
        return "Use set_reminder for this. "
    if any(w in lower for w in ["process", "task", "service", "running", "port", "disk", "cpu",
                                  "memory", "ram", "battery", "network", "ip address", "ping"]):
        return "Use run_terminal for this. Run a PowerShell command to get the information. "
    if any(w in lower for w in ["send email", "email to", "send a message"]):
        return "Use send_email for this. "
    if any(w in lower for w in ["create", "make a file", "new document", "new file"]):
        return "Use create_file for this. "
    if any(w in lower for w in ["open ", "launch ", "start "]):
        return "Use open_app for this. "
    if any(w in lower for w in ["close ", "quit "]):
        return "Use close_app for this. "
    if any(w in lower for w in ["minimize"]):
        return "Use minimize_app for this. "

    return (
        "Available tools: get_weather, get_forecast, get_time, get_news, "
        "play_music, open_app, toggle_setting, set_reminder, "
        "create_file, send_email, minimize_app, close_app, agent_task. "
    )
