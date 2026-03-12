"""
Interactive tool registrations — tools that present choices to the user.

Registers: ask_user_choice, ask_user_input
These tools let the LLM ask the user to pick from options or provide input
when the agent encounters multiple choices (accounts, items, settings, etc.).
"""

import logging

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ===================================================================
# Handler functions
# ===================================================================

def _handle_ask_user_choice(arguments, speak_fn=None):
    """Present numbered options and get user's choice."""
    question = arguments.get("question", "Please choose an option:")
    options = arguments.get("options", [])
    allow_auto = arguments.get("allow_auto_pick", True)

    if not options:
        return "Error: no options provided."
    if not isinstance(options, list):
        # LLM might pass a string — try to split
        if isinstance(options, str):
            options = [o.strip() for o in options.split(",") if o.strip()]
        else:
            return "Error: options must be a list."

    if len(options) == 1:
        return f"Only one option available: {options[0]}. Proceeding with it."

    from user_choice import prompt_choice
    idx = prompt_choice(
        question=question,
        options=options,
        speak_fn=speak_fn,
        allow_auto=allow_auto,
    )

    if idx is None:
        return "User cancelled the selection."

    chosen = options[idx]
    return f"User chose: {chosen}"


def _handle_ask_user_input(arguments, speak_fn=None):
    """Ask user for free-form text input."""
    question = arguments.get("question", "Please provide input:")
    sensitive = arguments.get("sensitive", False)

    from user_choice import prompt_input
    value = prompt_input(
        question=question,
        speak_fn=speak_fn,
        sensitive=sensitive,
    )

    if value is None:
        return "User cancelled the input."

    if sensitive:
        return "User provided the input (hidden for security)."
    return f"User entered: {value}"


def _handle_ask_yes_no(arguments, speak_fn=None):
    """Ask user a yes/no confirmation question."""
    question = arguments.get("question", "Should I proceed?")

    from user_choice import prompt_yes_no
    result = prompt_yes_no(
        question=question,
        speak_fn=speak_fn,
    )

    if result is True:
        return "User said yes."
    elif result is False:
        return "User said no."
    return "User didn't respond clearly."


# ===================================================================
# Registration
# ===================================================================

def register_interactive_tools(registry: ToolRegistry):
    """Register interactive user-choice tools."""

    # --- ask_user_choice ---
    registry.register(ToolSpec(
        name="ask_user_choice",
        description=(
            "Present multiple options to the user and let them choose. "
            "Use this when there are multiple choices (e.g., accounts, items, "
            "results, varieties) and the user should pick one. "
            "The user can say a number, the option name, or 'pick for me'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask (e.g., 'Which account would you like to use?')",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of options to present (e.g., ['john@gmail.com', 'work@gmail.com'])",
                },
                "allow_auto_pick": {
                    "type": "boolean",
                    "description": "Allow user to say 'pick for me' (default: true)",
                },
            },
            "required": ["question", "options"],
        },
        handler=_handle_ask_user_choice,
        safety="safe",
        aliases=["ask_choice", "ask_user", "present_options", "show_options",
                 "user_choice", "multiple_choice", "pick_option"],
        primary_arg="question",
        core=True,
        llm_enabled=True,
        requires_speak_fn=True,
    ))

    # --- ask_user_input ---
    registry.register(ToolSpec(
        name="ask_user_input",
        description=(
            "Ask the user for free-form text input. "
            "Use this when you need the user to type or say something "
            "(e.g., email address, password, search query, custom text). "
            "Set sensitive=true for passwords (won't be echoed)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask (e.g., 'What is your email address?')",
                },
                "sensitive": {
                    "type": "boolean",
                    "description": "If true, input won't be shown (for passwords). Default: false.",
                },
            },
            "required": ["question"],
        },
        handler=_handle_ask_user_input,
        safety="safe",
        aliases=["ask_input", "get_user_input", "request_input", "prompt_user"],
        primary_arg="question",
        core=True,
        llm_enabled=True,
        requires_speak_fn=True,
    ))

    # --- ask_yes_no ---
    registry.register(ToolSpec(
        name="ask_yes_no",
        description=(
            "Ask the user a yes/no question. "
            "Use this for confirmations before proceeding with an action."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The yes/no question (e.g., 'Should I use your Gmail account?')",
                },
            },
            "required": ["question"],
        },
        handler=_handle_ask_yes_no,
        safety="safe",
        aliases=["confirm", "ask_confirmation", "yes_no"],
        primary_arg="question",
        core=True,
        llm_enabled=True,
        requires_speak_fn=True,
    ))

    logger.info("Interactive tools registered: ask_user_choice, ask_user_input, ask_yes_no")
