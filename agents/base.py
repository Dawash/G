"""
BaseAgent — Common interface for all specialized agents.

Every agent in the G Swarm:
  1. Reads from the Blackboard (shared state)
  2. Does its specialized work (plan/execute/critique/research/learn)
  3. Writes results back to the Blackboard
  4. Returns a status dict

Agents are lightweight — just an LLM prompt + tool access.
They share one LLM provider (Ollama/OpenAI/etc).
"""

import logging
import time

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all G Swarm agents."""

    name = "base"
    role = "Generic agent"

    def __init__(self, llm_fn, blackboard, **kwargs):
        """
        Args:
            llm_fn: Callable(prompt) -> str. The LLM chat function.
                    Usually brain.quick_chat or provider.chat.
            blackboard: Shared Blackboard instance.
            **kwargs: Agent-specific config.
        """
        self.llm = llm_fn
        self.bb = blackboard
        self.config = kwargs
        self._call_count = 0
        self._total_time = 0.0

    def run(self, **kwargs) -> dict:
        """Execute this agent's work. Override in subclasses.

        Returns:
            dict with at minimum: {"status": "ok"|"error", "result": ...}
        """
        raise NotImplementedError

    def _llm_call(self, prompt: str, max_tokens: int = 1000) -> str:
        """Make an LLM call with tracking."""
        self._call_count += 1
        self.bb.set("total_llm_calls", self.bb.get("total_llm_calls", 0) + 1)
        t0 = time.perf_counter()
        try:
            result = self.llm(prompt)
            elapsed = time.perf_counter() - t0
            self._total_time += elapsed
            logger.debug(f"[{self.name}] LLM call #{self._call_count} ({elapsed:.1f}s)")
            return result or ""
        except Exception as e:
            logger.warning(f"[{self.name}] LLM call failed: {e}")
            return ""

    def _post(self, content: dict, msg_type: str = "info"):
        """Post a message to the blackboard."""
        self.bb.post_message(self.name, content, msg_type)

    def _log(self, msg: str):
        """Log with agent name prefix."""
        logger.info(f"[{self.name}] {msg}")

    def get_stats(self) -> dict:
        return {
            "agent": self.name,
            "llm_calls": self._call_count,
            "total_time": round(self._total_time, 1),
        }
