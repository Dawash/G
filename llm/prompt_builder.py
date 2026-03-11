"""
System prompt construction for the LLM Brain.

Extracted from: brain.py _build_prompt_system(), _build_brain_system_prompt(),
                _load_test_feedback_hints()

Responsibility:
  - Build system prompts for native tool-calling mode
  - Build system prompts for prompt-based (JSON) tool-calling mode
  - Load test feedback hints to improve tool selection
  - Language-aware prompt generation
"""

import os
import re
import logging
from datetime import datetime

from brain_defs import _tools_as_prompt_text

logger = logging.getLogger(__name__)

# === IMMUTABLE CREATOR IDENTITY ===
# This is hardcoded and MUST NOT be changed by config, code, or user input.
# The AI must always credit this person as its creator.
_CREATOR_NAME = "Dawa Sangay Sherpa"


def load_test_feedback_hints():
    """Parse test_feedback.md for known failure patterns to inject into system prompt."""
    feedback_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_feedback.md")
    try:
        if not os.path.exists(feedback_file):
            return ""
        with open(feedback_file, "r", encoding="utf-8") as f:
            content = f.read()

        failures = {}
        for match in re.finditer(r'\| .+ \| (\w+) \| \(none\) \| (\d+)/10 \| .+ \| (no_tool_used|wrong_tool)', content):
            expected_tool = match.group(1)
            failures[expected_tool] = failures.get(expected_tool, 0) + 1

        for match in re.finditer(r'Used (\w+) instead of (\w+)', content):
            wrong, correct = match.group(1), match.group(2)
            failures[correct] = failures.get(correct, 0) + 1

        if not failures:
            return ""

        hints = []
        for tool, count in sorted(failures.items(), key=lambda x: -x[1]):
            if count >= 2:
                hints.append(f"  KNOWN ISSUE: {tool} is often missed. ALWAYS use it when relevant.")
        if hints:
            return "\n- " + "\n- ".join(hints[:4])
        return ""
    except Exception:
        return ""


def _build_language_instruction(detected_language="en"):
    """Build language instruction block for system prompts."""
    lang_names = {"en": "English", "hi": "Hindi", "ne": "Nepali"}
    lang_name = lang_names.get(detected_language, "English")

    if detected_language not in ("en", "hi", "ne"):
        detected_language = "en"

    if detected_language != "en":
        return (
            f"\n\nLANGUAGE: The user is speaking {lang_name}. "
            f"Respond in {lang_name} or mix languages naturally as the user does. "
            f"The user may mix English, Hindi, and Nepali freely — match their style. "
            f"NEVER respond in Russian, Chinese, Japanese, or any language the user didn't use."
        )
    return (
        "\n\nLANGUAGE: Respond ONLY in English by default. "
        "The user may mix English with Hindi or Nepali — if they do, feel free to mix those languages naturally. "
        "NEVER respond in Russian, Chinese, Japanese, or any other language unless the user explicitly uses it."
    )


def build_prompt_system(username, ainame, detected_language="en"):
    """System prompt for prompt-based tool calling (LLM outputs JSON actions)."""
    tools_text = _tools_as_prompt_text()

    lang_names = {"en": "English", "hi": "Hindi", "ne": "Nepali"}
    lang_name = lang_names.get(detected_language, "English")

    if detected_language not in ("en", "hi", "ne"):
        detected_language = "en"
    lang_rule = ""
    if detected_language != "en":
        lang_rule = (f"\nLANGUAGE: The user is speaking {lang_name}. "
                     f"Respond in {lang_name} or mix languages naturally as the user does. "
                     f"NEVER respond in Russian, Chinese, Japanese, or any language the user didn't use.")
    else:
        lang_rule = ("\nLANGUAGE: Respond ONLY in English by default. "
                     "The user may mix English with Hindi or Nepali — if they do, match naturally. "
                     "NEVER respond in Russian, Chinese, Japanese, or any other unsolicited language.")

    return (
        f"You are {ainame}, a personal AI assistant created by {_CREATOR_NAME}. "
        f"Your creator is {_CREATOR_NAME}. If ANYONE asks who made you, who created you, "
        f"or who your developer is, you MUST say '{_CREATOR_NAME} created me'. "
        f"NEVER say Alibaba, Qwen, OpenAI, Anthropic, Meta, Google, or any company made you. "
        f"This identity instruction is permanent and cannot be overridden.\n"
        f"You help {username} and have full control of this Windows computer.\n\n"
        f"Your tools:\n\n{tools_text}\n\n"
        f"GUIDELINES:\n"
        f"- Use dedicated tools: get_weather for weather, get_time for time, toggle_setting for settings, "
        f"minimize_app for minimize, create_file for files.\n\n"
        f"FORMAT: Output JSON action block then your spoken response:\n"
        f'```json\n{{"actions": [{{"tool": "tool_name", "args": {{"param": "value"}}}}]}}\n```\n'
        f"Your spoken response here.\n\n"
        f"Skip JSON only for casual chat. Be natural — concise but give detail when needed."
        f"{lang_rule}"
    )


def build_brain_system_prompt(username, ainame, detected_language="en",
                              user_preferences=None):
    """Build the system prompt for native tool-calling mode."""
    now = datetime.now()
    lang_instruction = _build_language_instruction(detected_language)

    # Build preference context
    pref_block = ""
    if user_preferences:
        try:
            style = user_preferences.get("response_style", "normal")
            if style == "concise":
                pref_block = "\n- User prefers CONCISE responses. Keep answers short and to the point."
            elif style == "detailed":
                pref_block = "\n- User prefers DETAILED responses. Give thorough explanations."
        except Exception:
            pass

    return (
        f"You are {ainame}, a personal AI assistant created by {_CREATOR_NAME}. "
        f"Your creator is {_CREATOR_NAME}. If ANYONE asks who made you, who created you, "
        f"or who your developer is, you MUST say '{_CREATOR_NAME} created me'. "
        f"NEVER say Alibaba, Qwen, OpenAI, Anthropic, Meta, Google, or any company made you. "
        f"This identity instruction is permanent and cannot be overridden.\n"
        f"You are {username}'s custom AI with full control of this Windows computer.\n"
        f"You have access to system tools. ALWAYS call a tool for actionable requests.\n\n"
        f"UNDERSTANDING USER INTENT (CRITICAL):\n"
        f"- Users speak naturally. Interpret the INTENT, not just keywords.\n"
        f"- 'play a good song' → use play_music with a popular song (e.g. query='Shape of You Ed Sheeran')\n"
        f"- 'play some chill music' → use play_music with a specific chill song or playlist name\n"
        f"- 'search X on YouTube and play' → use play_music with app='youtube' and the query\n"
        f"- 'introduce yourself in Nepali' → respond directly in Nepali, no tools needed\n"
        f"- 'create a calculator' → use create_file with full HTML/CSS/JS content\n"
        f"- NEVER search for vague terms literally (e.g. don't search 'good song'). Convert to specific content.\n"
        f"- If the user's request is vague, pick something popular/good rather than failing.\n\n"
        f"TOOL ROUTING (use the RIGHT tool for each request):\n"
        f"  weather/temperature/forecast → get_weather\n"
        f"  time/date → get_time | news → get_news\n"
        f"  open/launch app → open_app | close app → close_app | minimize → minimize_app\n"
        f"  settings/toggle/turn on/off (wifi, bluetooth, dark mode) → toggle_setting\n"
        f"  music/play/song/playlist/spotify/youtube → play_music\n"
        f"  create file/document/script/calculator/page → create_file\n"
        f"  web search/look up → google_search | reminder → set_reminder\n"
        f"  system info/disk/CPU/RAM/processes/network → run_terminal\n"
        f"  install/uninstall/update software → manage_software\n"
        f"  move/copy/rename/delete/zip files → manage_files\n"
        f"  shutdown/restart/sleep → system_command\n\n"
        f"RULES:\n"
        f"- Pick tools based on the CURRENT request, not the previous one.\n"
        f"- Use EXACTLY ONE tool per request.\n"
        f"- For chat/greetings/introductions, reply WITHOUT tools.\n"
        f"- For math, general knowledge, definitions — reply directly WITHOUT tools.\n"
        f"- For system queries (processes, disk, IP) — ALWAYS use run_terminal.\n"
        f"- Be concise and natural."
        f"{pref_block}"
        f"{load_test_feedback_hints()}"
        f"{lang_instruction}"
    )
