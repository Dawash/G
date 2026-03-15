"""
Smart Search Plugin — enhanced search with context awareness.

Features:
  - Remembers recent searches for follow-up questions
  - Detects when user wants to search a specific site (amazon, youtube, etc.)
  - Provides search suggestions based on past searches
  - Quick definitions via LLM
"""

from plugins.base import BasePlugin, PluginIntent, PluginTool


class SmartSearchPlugin(BasePlugin):
    name = "smart_search"
    description = "Context-aware search with site routing and definitions"
    version = "1.0"
    author = "G Assistant"

    def get_intents(self):
        return [
            PluginIntent(
                r"(?:define|definition of|what does .+ mean|meaning of)\s+(.+)",
                self.quick_define,
                priority=55,
                description="Quick word/phrase definition",
            ),
            PluginIntent(
                r"(?:translate|how do you say)\s+(.+?)\s+(?:in|to)\s+(\w+)",
                self.quick_translate,
                priority=55,
                description="Quick translation",
            ),
        ]

    def get_tools(self):
        return [
            PluginTool(
                name="quick_define",
                description="Get a quick definition of a word or phrase. "
                            "Use when user asks 'what does X mean' or 'define X'.",
                parameters={
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "The word or phrase to define",
                        },
                    },
                    "required": ["term"],
                },
                handler=self._handle_define,
            ),
        ]

    def quick_define(self, text, match):
        """Handle definition requests via intent matching."""
        import re
        term = re.sub(r'^(?:define|definition of|meaning of)\s+', '', text, flags=re.I)
        term = re.sub(r'^what does\s+|\s+mean$', '', term, flags=re.I).strip()
        if not term:
            return None

        # Use LLM for definition
        definition = self.quick_chat(
            f"Define '{term}' in 1-2 clear sentences. Be concise and accurate. "
            f"If it's a common word, give the most relevant meaning."
        )
        if definition:
            # Remember for follow-up
            self.remember("last_search", term)
            return definition
        return f"'{term}' — I couldn't find a definition right now."

    def quick_translate(self, text, match):
        """Handle translation requests."""
        phrase = match.group(1).strip()
        target_lang = match.group(2).strip()

        translation = self.quick_chat(
            f"Translate '{phrase}' to {target_lang}. "
            f"Just give the translation, then pronunciation in parentheses if non-Latin script. "
            f"Keep it brief."
        )
        return translation or f"I couldn't translate '{phrase}' to {target_lang}."

    def _handle_define(self, arguments):
        """LLM tool handler for definitions."""
        term = arguments.get("term", "")
        if not term:
            return "Please specify a term to define."
        definition = self.quick_chat(
            f"Define '{term}' concisely in 1-2 sentences."
        )
        return definition or f"Couldn't define '{term}'."
