"""
LLM response post-processing and cleanup.

Extracted from: brain.py Brain._sanitize_response(), Brain._is_llm_refusal(),
                Brain._suggest_tool_for_retry()

Responsibility:
  - Sanitize LLM output (strip special tokens, code fences, markdown artifacts)
  - Strip non-Latin script leaks (CJK, Cyrillic) before TTS
  - Detect LLM refusal patterns ("I'm an AI, I can't...")
  - Suggest correct tool when LLM refuses to use tools
"""

import re
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Non-Latin script stripping (CJK / Cyrillic leak from qwen2.5)
# ---------------------------------------------------------------------------

# Regex matching contiguous runs of characters in scripts we want to strip.
# Covers: CJK Unified Ideographs, CJK Extension A/B, CJK Compatibility,
#         Hiragana, Katakana, Hangul, Cyrillic, Arabic, Thai, Devanagari,
#         and assorted CJK punctuation / fullwidth forms.
#
# Deliberately EXCLUDES:
#   - Basic Latin (U+0020-007F)
#   - Latin Extended-A/B and Latin Supplement (U+0080-024F) — accented chars
#   - General punctuation, currency symbols, math symbols
#   - Common emoji ranges (left alone; TTS ignores them)
_NON_LATIN_RANGES = (
    r'[\u2E80-\u2FDF'   # CJK Radicals Supplement, Kangxi Radicals
    r'\u3000-\u303F'     # CJK Symbols and Punctuation (ideographic comma, etc.)
    r'\u3040-\u309F'     # Hiragana
    r'\u30A0-\u30FF'     # Katakana
    r'\u3100-\u312F'     # Bopomofo
    r'\u3130-\u318F'     # Hangul Compatibility Jamo
    r'\u3200-\u32FF'     # Enclosed CJK Letters
    r'\u3300-\u33FF'     # CJK Compatibility
    r'\u3400-\u4DBF'     # CJK Unified Ideographs Extension A
    r'\u4E00-\u9FFF'     # CJK Unified Ideographs
    r'\uA960-\uA97F'     # Hangul Jamo Extended-A
    r'\uAC00-\uD7AF'     # Hangul Syllables
    r'\uD7B0-\uD7FF'     # Hangul Jamo Extended-B
    r'\uF900-\uFAFF'     # CJK Compatibility Ideographs
    r'\uFE30-\uFE4F'     # CJK Compatibility Forms
    r'\uFF00-\uFFEF'     # Fullwidth Forms (fullwidth Latin, halfwidth Katakana)
    r'\u0400-\u04FF'     # Cyrillic
    r'\u0500-\u052F'     # Cyrillic Supplement
    r'\u0600-\u06FF'     # Arabic
    r'\u0900-\u097F'     # Devanagari
    r'\u0E00-\u0E7F'     # Thai
    r']'
)

_RE_NON_LATIN = re.compile(_NON_LATIN_RANGES + r'+')


def sanitize_for_speech(text):
    """Strip non-English/non-Latin characters from LLM text before TTS.

    qwen2.5 randomly injects Chinese (CJK) mid-sentence into English
    responses.  This function removes those sequences so TTS only speaks
    intelligible text.

    Preserves:
      - ASCII letters, digits, punctuation, whitespace
      - Accented Latin characters (cafe, naive, Dusseldorf)
      - URLs and file paths (no CJK in those normally)
      - Numbers that were adjacent to CJK (e.g. "12" in "预计12英寸" -> "12")

    Args:
        text: Raw LLM response string.

    Returns:
        Cleaned string safe for English TTS, or empty string if input was
        None/empty.
    """
    if not text:
        return text or ""

    # Strip all non-Latin script runs
    cleaned = _RE_NON_LATIN.sub(' ', text)

    # Clean up artifacts left by stripping:
    #   - Multiple consecutive spaces
    cleaned = re.sub(r'  +', ' ', cleaned)
    #   - Space before punctuation: "Hello , world" -> "Hello, world"
    cleaned = re.sub(r'\s+([.,;:!?\)])', r'\1', cleaned)
    #   - Space after opening paren: "( hello" -> "(hello"
    cleaned = re.sub(r'(\()\s+', r'\1', cleaned)
    #   - Repeated punctuation: ",," or ",, " or ". ." -> single punctuation
    cleaned = re.sub(r'([.,;:!?])\s*\1+', r'\1', cleaned)
    #   - Orphaned punctuation at start of text or after whitespace
    cleaned = re.sub(r'(?:^|(?<=\s))[.,;:!?]+(?=\s|$)', '', cleaned)
    #   - Leading/trailing whitespace per line
    cleaned = '\n'.join(line.strip() for line in cleaned.split('\n'))
    #   - Collapse multiple spaces again after all cleanup
    cleaned = re.sub(r'  +', ' ', cleaned)

    return cleaned.strip()


def sanitize_response(text):
    """Remove LLM artifacts (special tokens, leftover JSON) from spoken text."""
    if not text:
        return text
    # Strip problematic Unicode characters that crash Windows console encoding
    # Zero-width spaces, non-breaking spaces, RTL/LTR marks, etc.
    text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2028\u2029]', '', text)
    # Replace Unicode arrows/dashes with ASCII equivalents
    text = text.replace('\u2192', '->').replace('\u2190', '<-')
    text = text.replace('\u2014', '-').replace('\u2013', '-')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
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
        'times': 'x', 'approx': '~', 'cdot': '.', 'div': '/',
        'sqrt': 'sqrt', 'pm': '+/-', 'leq': '<=', 'geq': '>=', 'neq': '!=',
    }.get(m.group(1), m.group(1)), text)
    # Final safety: strip invisible/control characters that crash Windows console
    # Keep visible Unicode (Nepali, Japanese, etc.) but remove zero-width and control chars
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
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
