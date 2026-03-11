"""
Base adapter and shared types for provider-agnostic tool call extraction.

ActionResult is the unified output: a list of ToolCalls + the LLM's text response.
"""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A single tool call extracted from LLM output."""
    name: str
    arguments: dict = field(default_factory=dict)
    confidence: float = 1.0  # How confident we are this is correct
    source: str = "native"   # "native", "json", "prompt"

    def __repr__(self):
        args_str = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"ToolCall({self.name}({args_str}), conf={self.confidence})"


@dataclass
class ActionResult:
    """Unified result of parsing an LLM response for tool calls.

    Combines tool calls and the text response into one structure.
    Used by Brain to process LLM output regardless of provider.
    """
    tool_calls: list = field(default_factory=list)  # List of ToolCall
    text: str = ""           # LLM's text/spoken response
    raw_response: dict = field(default_factory=dict)  # Original response for debugging
    extraction_method: str = "none"  # "native", "json", "prompt", "none"

    @property
    def has_tool_calls(self):
        return bool(self.tool_calls)

    @property
    def primary_tool(self):
        """Get the first/main tool call."""
        return self.tool_calls[0] if self.tool_calls else None


class ProviderAdapter:
    """Base class for provider-specific tool call extraction.

    Subclasses implement extract() to parse the raw LLM response
    and return an ActionResult with normalized ToolCalls.
    """

    provider_name: str = "base"

    def extract(self, response, tool_definitions=None):
        """Extract tool calls from a raw LLM response.

        Args:
            response: The raw response dict from the provider's API.
            tool_definitions: List of tool definitions (for validation).

        Returns:
            ActionResult with extracted tool calls and text.
        """
        raise NotImplementedError

    def format_tools(self, tool_definitions):
        """Format tool definitions for this provider's API.

        Default: return as-is (OpenAI format is the common standard).
        """
        return tool_definitions

    def supports_native_tools(self):
        """Whether this provider supports native tool calling."""
        return True

    def _validate_tool_name(self, name, tool_definitions):
        """Check if a tool name is valid."""
        if not tool_definitions:
            return True
        valid_names = set()
        for td in tool_definitions:
            if isinstance(td, dict):
                fn = td.get("function", td)
                valid_names.add(fn.get("name", ""))
            else:
                valid_names.add(str(td))
        return name in valid_names
