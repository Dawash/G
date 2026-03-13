"""
Interactive multi-choice system for user decisions.

When the agent encounters a situation with multiple options (Gmail accounts,
pizza varieties, search results, login methods, etc.), this module presents
numbered choices and collects user input via voice or keyboard.

Usage:
    from user_choice import prompt_choice, prompt_input

    # Multiple choice
    idx = prompt_choice(
        "Which Gmail account?",
        ["john@gmail.com", "work@gmail.com", "personal@gmail.com"],
        speak_fn=speak,
    )
    # idx = 0, 1, 2, or AUTO_PICK (-1)

    # Free-form input
    value = prompt_input("Enter your email address:", speak_fn=speak)
"""

import logging
import re

logger = logging.getLogger(__name__)

# Sentinel: user said "pick for me" / "you choose" / "surprise me"
AUTO_PICK = -1

# Patterns that mean "you decide for me"
_AUTO_PICK_PHRASES = [
    "pick for me", "pick it for me", "pick yourself", "pick by yourself",
    "pick any", "pick anyone", "pick whatever",
    "you pick", "you choose", "you decide", "your choice",
    "surprise me", "anything", "whatever", "don't care", "doesnt matter",
    "doesn't matter", "i don't care", "auto", "random",
    "dealer's choice", "dealers choice", "up to you",
    "just pick one", "just pick", "just choose", "just any", "just use any",
    "any of them", "any one", "any will do", "any is fine",
    "use any", "use whichever", "use whatever",
    "go ahead and pick", "go ahead", "let me know", "you can pick",
    "select any", "choose any", "go with any",
]

# Ordinal words → index
_ORDINALS = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
    "sixth": 5, "6th": 5,
    "seventh": 6, "7th": 6,
    "eighth": 7, "8th": 7,
    "ninth": 8, "9th": 8,
    "tenth": 9, "10th": 9,
    "last": -2,  # Special: last item
}

# Cancel patterns
_CANCEL_PHRASES = [
    "cancel", "stop", "never mind", "nevermind", "forget it",
    "go back", "abort", "quit", "none", "no thanks",
]


def _get_input(speak_fn=None, timeout_s=30):
    """Get user input via voice or keyboard (hybrid mode).

    Returns user's text response, or None on timeout/failure.
    """
    try:
        from speech import listen
        result = listen()
        return result
    except Exception as e:
        logger.debug(f"Voice input failed: {e}")

    # Fallback: text input
    try:
        return input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _parse_choice(response, options):
    """Parse a user response into a choice index.

    Handles:
    - Numbers: "1", "2", "3"
    - Ordinals: "first", "second", "the third one"
    - Option text match: "pepperoni", "john@gmail.com"
    - Auto-pick phrases: "you choose", "pick for me"
    - Cancel phrases: "cancel", "never mind"

    Returns:
        int: 0-based index, AUTO_PICK (-1), or None (unrecognized/cancel)
    """
    if not response:
        return None

    text = response.lower().strip().rstrip(".,!?")

    # Check cancel
    for phrase in _CANCEL_PHRASES:
        if phrase in text:
            return None

    # Check auto-pick
    for phrase in _AUTO_PICK_PHRASES:
        if phrase in text:
            return AUTO_PICK

    # Check plain number: "1", "2", "3"
    m = re.match(r'^(\d+)$', text)
    if m:
        idx = int(m.group(1)) - 1  # 1-based to 0-based
        if 0 <= idx < len(options):
            return idx

    # Check "number N", "option N", "choice N"
    m = re.search(r'(?:number|option|choice|item)\s*(\d+)', text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return idx

    # Check ordinals: "first", "the second one", "third"
    for word, idx in _ORDINALS.items():
        if word in text:
            if idx == -2:  # "last"
                return len(options) - 1
            if 0 <= idx < len(options):
                return idx

    # Check "the Nth one" pattern
    m = re.search(r'the\s+(\d+)(?:st|nd|rd|th)?\s+one', text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return idx

    # Fuzzy match against option text
    from difflib import SequenceMatcher
    best_idx = None
    best_ratio = 0.0
    for i, opt in enumerate(options):
        opt_lower = str(opt).lower()
        # Exact substring match
        if text in opt_lower or opt_lower in text:
            return i
        # Fuzzy match
        ratio = SequenceMatcher(None, text, opt_lower).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    if best_ratio >= 0.6 and best_idx is not None:
        return best_idx

    return None  # Unrecognized


def prompt_choice(question, options, speak_fn=None, allow_auto=True,
                  auto_pick_index=0, max_retries=2):
    """Present numbered options and get user's choice.

    Args:
        question: The question to ask (e.g., "Which account?")
        options: List of option strings
        speak_fn: TTS function (optional)
        allow_auto: Whether "pick for me" is allowed
        auto_pick_index: Which option to auto-pick (default: first)
        max_retries: Max times to re-prompt on invalid input

    Returns:
        int: 0-based index of chosen option, or None if cancelled.
        If user says "pick for me" and allow_auto=True, returns auto_pick_index.
    """
    if not options:
        return None
    if len(options) == 1:
        return 0  # Only one option, auto-select

    # Build the prompt text
    lines = [question]
    for i, opt in enumerate(options, 1):
        lines.append(f"  {i}. {opt}")

    suffix = " Say a number"
    if allow_auto:
        suffix += ", or say 'pick for me' to let me choose"
    suffix += "."
    lines.append(suffix)

    prompt_text = "\n".join(lines)
    # For TTS: speak a condensed version
    tts_text = f"{question} "
    for i, opt in enumerate(options, 1):
        tts_text += f"Option {i}: {opt}. "
    if allow_auto:
        tts_text += "Say a number, or say pick for me."
    else:
        tts_text += "Say a number."

    # Display in console
    print(f"\n{'─' * 45}")
    print(prompt_text)
    print(f"{'─' * 45}")

    # Speak the question
    if speak_fn:
        try:
            speak_fn(tts_text)
        except Exception:
            pass

    # Get user response
    for attempt in range(max_retries + 1):
        response = _get_input(speak_fn)

        if not response:
            if attempt < max_retries:
                msg = "I didn't catch that. Please say a number."
                print(f"  {msg}")
                if speak_fn:
                    try:
                        speak_fn(msg)
                    except Exception:
                        pass
                continue
            return None

        idx = _parse_choice(response, options)

        if idx is None:
            # Cancelled or unrecognized
            if any(p in response.lower() for p in _CANCEL_PHRASES):
                print("  Cancelled.")
                return None
            if attempt < max_retries:
                msg = f"I didn't understand '{response}'. Please say a number from 1 to {len(options)}."
                print(f"  {msg}")
                if speak_fn:
                    try:
                        speak_fn(msg)
                    except Exception:
                        pass
                continue
            return None

        if idx == AUTO_PICK:
            if allow_auto:
                chosen = auto_pick_index if 0 <= auto_pick_index < len(options) else 0
                msg = f"I'll go with option {chosen + 1}: {options[chosen]}"
                print(f"  {msg}")
                if speak_fn:
                    try:
                        speak_fn(msg)
                    except Exception:
                        pass
                return chosen
            # Auto not allowed, re-prompt
            msg = "Please choose a specific option."
            print(f"  {msg}")
            if speak_fn:
                try:
                    speak_fn(msg)
                except Exception:
                    pass
            continue

        # Valid choice
        msg = f"Got it: {options[idx]}"
        print(f"  {msg}")
        logger.info(f"User chose option {idx + 1}: {options[idx]}")
        return idx

    return None


def prompt_input(question, speak_fn=None, sensitive=False, max_retries=2):
    """Ask user for free-form text input (e.g., email, password).

    Args:
        question: The question to ask
        speak_fn: TTS function
        sensitive: If True, don't echo input (for passwords)
        max_retries: Max retries on empty input

    Returns:
        str: User's input text, or None if cancelled
    """
    print(f"\n  {question}")
    if speak_fn:
        try:
            speak_fn(question)
        except Exception:
            pass

    for attempt in range(max_retries + 1):
        if sensitive:
            # Password: keyboard only, no echo
            try:
                import getpass
                value = getpass.getpass("  > ")
            except (EOFError, KeyboardInterrupt):
                return None
        else:
            value = _get_input(speak_fn)

        if not value:
            if attempt < max_retries:
                msg = "I didn't catch that. Please try again."
                print(f"  {msg}")
                if speak_fn:
                    try:
                        speak_fn(msg)
                    except Exception:
                        pass
                continue
            return None

        # Check cancel
        if value.lower().strip() in ("cancel", "never mind", "nevermind", "stop"):
            print("  Cancelled.")
            return None

        return value.strip()

    return None


def prompt_yes_no(question, speak_fn=None, default=None):
    """Ask a yes/no question.

    Args:
        question: The question
        speak_fn: TTS function
        default: Default answer if user is unclear (True/False/None)

    Returns:
        bool: True for yes, False for no, None if cancelled
    """
    full_q = f"{question} Say yes or no."
    print(f"\n  {full_q}")
    if speak_fn:
        try:
            speak_fn(full_q)
        except Exception:
            pass

    response = _get_input(speak_fn)
    if not response:
        return default

    text = response.lower().strip()
    if any(w in text for w in ["yes", "yeah", "yep", "sure", "go ahead",
                                "do it", "confirm", "okay", "ok", "affirmative"]):
        return True
    if any(w in text for w in ["no", "nah", "nope", "don't", "cancel", "stop"]):
        return False

    return default
