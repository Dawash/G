"""
Meta-command and special command routing.

Extracted from: assistant.py (meta-commands, exit/connect/provider/self-test detection)

Responsibility:
  - Detect and handle: skip, shorter, repeat, undo, more detail, emergency_stop
  - Detect correction patterns ("No, I said X not Y")
  - Detect exit commands (quit/exit/bye)
  - Detect connection commands (disconnect/connect)
  - Detect provider switch commands
  - Detect self-test requests
  - Speech correction for known terms
  - These bypass the Brain entirely -- no LLM needed
"""

import re


# --- Meta-commands (pre-brain, no API needed) ---

_META_COMMANDS = {
    "skip": re.compile(r'^(skip|stop|shut up|be quiet|enough|next)$', re.I),
    "shorter": re.compile(r'^(shorter|too long|brief|briefly|summarize that|keep it short)$', re.I),
    "more_detail": re.compile(r'^(more detail|tell me more|elaborate|expand on that|go on|explain more)$', re.I),
    "repeat": re.compile(r'^(repeat|say that again|what did you say|come again|repeat that)$', re.I),
    "undo": re.compile(r'^(undo|cancel that|undo that|take that back|reverse that|revert)$', re.I),
    "emergency_stop": re.compile(r'^(stop everything|emergency stop|halt|abort|cancel everything|stop all)$', re.I),
}

_CORRECTION_RE = re.compile(r'no[,.]?\s+i\s+said\s+(.+)', re.I)


def detect_meta_command(text):
    """Check if text is a meta-command.

    Returns:
        str: command name ("skip", "shorter", "repeat", "undo", "more_detail", "emergency_stop")
        tuple: ("correction", corrected_text) for speech corrections
        None: not a meta-command
    """
    t = text.strip()
    for cmd, pattern in _META_COMMANDS.items():
        if pattern.match(t):
            return cmd
    m = _CORRECTION_RE.search(t)
    if m:
        corrected = m.group(1).strip()
        # Persist correction for speech learning
        try:
            from memory import MemoryStore
            if not hasattr(detect_meta_command, '_mem'):
                detect_meta_command._mem = MemoryStore()
            detect_meta_command._mem.remember("speech_corrections", corrected.lower(), t)
        except Exception:
            pass
        return ("correction", corrected)
    return None


# --- Exit commands ---

_EXIT_WORDS = frozenset(("quit", "exit", "bye", "see ya", "goodbye"))


def is_exit_command(text):
    """Check if text is an exit/goodbye command."""
    return text.lower().strip() in _EXIT_WORDS


# --- Connection commands ---

def is_connection_command(text):
    """Check if user wants to disconnect/connect.

    Returns:
        "disconnect", "connect", or None.
    """
    t = text.lower().strip()
    if t == "disconnect":
        return "disconnect"
    if "connect me" in t:
        return "connect"
    return None


# --- Provider switch detection ---

_PROVIDER_NAMES = {"ollama", "openai", "anthropic", "openrouter"}
_OLLAMA_SOUNDS = {
    "olama", "alama", "alarma", "olamma", "ollamma",
    "oh lama", "o lama", "allama", "ulama", "llama",
    "ohlama", "olema", "alema",
}


def check_provider_switch(text):
    """Check if user wants to switch AI provider. Fuzzy matches 'ollama'.

    Returns:
        Provider name string, or None.
    """
    t = text.lower()
    switch_match = re.search(r"\b(?:switch\s*(?:to)?|use|change\s*to)\s+(\w+)", t)
    if not switch_match:
        return None

    spoken = switch_match.group(1)
    if spoken in _PROVIDER_NAMES:
        return spoken

    if spoken in _OLLAMA_SOUNDS:
        return "ollama"
    if re.match(r"^(ol|ll)", spoken) and len(spoken) >= 4:
        return "ollama"
    return None


# --- Self-test detection ---

_SELF_TEST_PHRASES = (
    "test yourself", "self test", "self-test", "run diagnostics",
    "check yourself", "debug yourself", "check for bug",
    "check the system", "system check", "health check",
)


def is_self_test_request(text):
    """Check if user wants to run diagnostics."""
    t = text.lower()
    return any(p in t for p in _SELF_TEST_PHRASES)


# --- Speech corrections ---

_SPEECH_CORRECTIONS = {}
_SPEECH_CORRECTIONS_COMPILED = [
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in _SPEECH_CORRECTIONS.items()
]


def correct_speech(text):
    """Fix common speech recognition errors for project-specific terms."""
    for pattern, replacement in _SPEECH_CORRECTIONS_COMPILED:
        text = pattern.sub(replacement, text)
    return text
