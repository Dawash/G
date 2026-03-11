"""
Response output and speech dispatch.

Extracted from: assistant.py (_say, _truncate_for_speech, _llm_response)

Responsibility:
  - Print and speak responses
  - Detect code-heavy responses and truncate for TTS
  - Generate fresh LLM responses for any situation (never canned text)
  - Handle text mode vs voice mode output differences
  - Fast local fallbacks for predictable situations (avoids 374ms LLM call)
"""

import os
import logging
import random

logger = logging.getLogger(__name__)

# Fast local responses for predictable situations — avoids ~374ms LLM call each.
# Multiple options per situation for variety; randomly selected.
_FAST_RESPONSES = {
    "wake": [
        "Hey! What do you need?",
        "I'm here! What's up?",
        "Hey there! What can I do?",
        "I'm listening! Go ahead.",
        "What's on your mind?",
    ],
    "farewell": [
        "See you later!",
        "Take care!",
        "Goodbye! Have a great one.",
        "See you soon!",
        "Catch you later!",
    ],
    "disconnect": [
        "Going offline. Local commands still work!",
        "Offline mode. I can still do local things.",
        "Disconnected. Still here for local commands.",
    ],
    "connect": [
        "Back online! What do you need?",
        "Reconnected! Ready to go.",
        "Online again. How can I help?",
    ],
    "self_test": [
        "Running diagnostics now...",
        "Starting system check...",
        "Let me run the self-test...",
    ],
    "restart": [
        "Restarting now, be right back!",
        "Restarting... see you in a moment.",
    ],
}


def _fast_response(situation_key):
    """Return a random fast response for a known situation, or None."""
    options = _FAST_RESPONSES.get(situation_key)
    if options:
        return random.choice(options)
    return None


def say(ainame, text, speak_interruptible_fn):
    """Print and speak a response. Returns interrupted text if user barges in.

    Detects code/long responses and speaks only a brief summary.
    In text mode: logs response, writes minimal marker to stdout.

    Args:
        ainame: AI assistant name for display.
        text: Response text to output.
        speak_interruptible_fn: Function that speaks text and returns
                                interrupted user input (or None).

    Returns:
        str or None: User's barge-in text if they interrupted, else None.
    """
    if os.environ.get("G_INPUT_MODE", "").lower() == "text":
        logger.info(f"RESPONSE: {text}")
        try:
            resp_line = f"{ainame}: {text[:200]}\n"
            os.write(1, resp_line.encode("utf-8", errors="replace"))
        except OSError:
            pass
        return None

    print(f"{ainame}: {text}")
    speak_text = truncate_for_speech(text)
    interrupted = speak_interruptible_fn(speak_text)
    return interrupted


def truncate_for_speech(text):
    """Truncate long or code-heavy responses for TTS. Full text is still printed."""
    code_indicators = [
        '```', '<!DOCTYPE', '<html', '<div', 'function ', 'def ', 'import ',
        '{', '}', 'const ', 'var ', 'let ', '.addEventListener',
        'document.', 'console.log', '#include', 'class ',
    ]
    code_count = sum(1 for ind in code_indicators if ind in text)

    if code_count >= 3:
        lines = text.split('\n')
        prose_lines = []
        for line in lines:
            stripped = line.strip()
            if (stripped.startswith(('```', '<', '{', '}', '//', '#', 'import ', 'from ',
                                    'def ', 'class ', 'const ', 'var ', 'let ', 'if ',
                                    'for ', 'while ', 'return ', '<!--'))
                    or stripped.endswith(('{', '}', ');', '};', ','))
                    or '=' in stripped and not stripped.startswith(('Note', 'This', 'I ',
                                                                   'You', 'The', 'It'))
                    or stripped.startswith(('**', '  '))):
                continue
            if stripped:
                prose_lines.append(stripped)

        if prose_lines:
            summary = ' '.join(prose_lines[:5])
            if len(summary) > 300:
                summary = summary[:300].rsplit(' ', 1)[0] + '.'
            return summary
        return "I've created the code. Check the console for details."

    if len(text) > 500:
        truncated = text[:300].rsplit(' ', 1)[0]
        remaining = len(text) - len(truncated)
        return truncated + f"... and {remaining} more characters. Check the console for the full response."

    return text


def llm_response(brain, situation, user_input, uname, fast_key=None):
    """Generate a response for a situation.

    Uses fast local fallback for known predictable situations to avoid
    the ~374ms LLM round-trip. Falls back to LLM for novel situations.

    Args:
        brain: Brain instance (may be None or have dead key).
        situation: Context description for the LLM.
        user_input: What the user said.
        uname: Username.
        fast_key: Optional key into _FAST_RESPONSES for instant response.

    Returns:
        str: Response text.
    """
    # Fast path: predictable situations don't need an LLM call
    if fast_key:
        fast = _fast_response(fast_key)
        if fast:
            try:
                from core.metrics import metrics
                metrics.increment("llm_calls_saved")
            except Exception:
                pass
            return fast

    if brain and not brain.key_is_dead:
        prompt = (
            f"User '{uname}' said: '{user_input}'. "
            f"Context: {situation}. "
            f"Give a brief, natural spoken response (1 sentence max). "
            f"Be warm and human-like, never robotic."
        )
        try:
            response = brain.quick_chat(prompt)
            if response:
                return response
        except Exception:
            pass
    return situation
