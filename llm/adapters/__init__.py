"""
Provider adapters — unified tool-call extraction across LLM providers.

Phase 18: Each provider returns tool calls in a different format.
Adapters normalize extraction so Brain doesn't need provider-specific code.

Supported providers:
  - Ollama (native tool calling + JSON fallback + prompt-based)
  - OpenAI (function_call / tool_calls in response)
  - Anthropic (tool_use content blocks)
  - OpenRouter (follows OpenAI format)
"""

from llm.adapters.base import ProviderAdapter, ActionResult, ToolCall
from llm.adapters.ollama_adapter import OllamaAdapter
from llm.adapters.openai_adapter import OpenAIAdapter

__all__ = [
    "ProviderAdapter", "ActionResult", "ToolCall",
    "OllamaAdapter", "OpenAIAdapter",
    "get_adapter",
]


def get_adapter(provider_name):
    """Get the right adapter for a provider.

    Args:
        provider_name: "ollama", "openai", "anthropic", "openrouter"

    Returns:
        ProviderAdapter instance.
    """
    name = provider_name.lower()
    if name == "ollama":
        return OllamaAdapter()
    elif name in ("openai", "openrouter"):
        return OpenAIAdapter()
    elif name == "anthropic":
        from llm.adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter()
    # Default: OpenAI-style (most common)
    return OpenAIAdapter()
