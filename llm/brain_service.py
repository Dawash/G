"""
LLM Brain service — facade/coordinator over extracted modules.

Coordinates:
  - llm.context_manager.ContextManager — conversation context + topic tracking
  - llm.mode_classifier — request classification (quick/agent/research)
  - llm.response_builder — response sanitization and cleanup
  - llm.prompt_builder — system prompt construction

The Brain class (brain.py) delegates to BrainService for context management,
mode classification, and response cleanup. Tool execution remains in brain.py
for now.

Future: BrainService will absorb more of Brain's think() pipeline
(provider invocation, tool-calling orchestration, etc.).
"""

import logging

from llm.context_manager import ContextManager
from llm.mode_classifier import classify_mode, ModeDecision
from llm.response_builder import sanitize_response, is_llm_refusal, suggest_tool_for_retry
from llm.prompt_builder import build_brain_system_prompt, build_prompt_system

logger = logging.getLogger(__name__)


class BrainService:
    """Facade over extracted LLM modules.

    Provides unified access to context management, mode classification,
    and response processing. Brain (brain.py) creates a BrainService
    and delegates to it.
    """

    def __init__(self, username, ainame, max_context=6):
        self.username = username
        self.ainame = ainame
        self.ctx = ContextManager(max_context=max_context)

    def classify(self, user_input, quick_chat_fn=None):
        """Classify user input into a mode (quick/agent/research)."""
        return classify_mode(user_input, quick_chat_fn=quick_chat_fn)

    def sanitize(self, text):
        """Clean up LLM response text."""
        return sanitize_response(text)

    def is_refusal(self, text):
        """Check if LLM output is a refusal to use tools."""
        return is_llm_refusal(text)

    def suggest_tool(self, user_msg):
        """Suggest the right tool when LLM refuses."""
        return suggest_tool_for_retry(user_msg)

    def build_system_prompt(self, native_tools=True, detected_language="en"):
        """Build the appropriate system prompt."""
        if native_tools:
            return build_brain_system_prompt(
                self.username, self.ainame, detected_language=detected_language)
        return build_prompt_system(
            self.username, self.ainame, detected_language=detected_language)

    def prepare_context(self, user_input, idle_threshold=120):
        """Pre-think context preparation: idle check + topic update + ambient context.

        Returns:
            tuple: (should_reset, ambient_context_str)
                should_reset: True if caller should call reset_context()
                ambient_context_str: Context to inject into system prompt
        """
        should_reset = self.ctx.check_idle_reset(idle_threshold)
        self.ctx.update_topic(user_input)
        ambient = self.ctx.get_ambient_context(user_input)
        return should_reset, ambient
