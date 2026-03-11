"""
Anthropic adapter — extracts tool calls from Claude's Messages API responses.

Anthropic returns tool use in content blocks:
  response.content = [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
  ]
"""

import logging

from llm.adapters.base import ProviderAdapter, ActionResult, ToolCall

logger = logging.getLogger(__name__)


class AnthropicAdapter(ProviderAdapter):
    """Adapter for Anthropic Messages API responses."""

    provider_name = "anthropic"

    def extract(self, response, tool_definitions=None):
        """Extract tool calls from Anthropic response."""
        if not response:
            return ActionResult()

        # Handle dict format
        if isinstance(response, dict):
            content_blocks = response.get("content", [])
        else:
            # Handle Anthropic SDK Message object
            try:
                content_blocks = response.content
            except AttributeError:
                return ActionResult()

        text_parts = []
        calls = []

        for block in content_blocks:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    calls.append(ToolCall(
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                        confidence=1.0,
                        source="native",
                    ))
            else:
                # SDK ContentBlock objects
                try:
                    if block.type == "text":
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        calls.append(ToolCall(
                            name=block.name,
                            arguments=block.input or {},
                            confidence=1.0,
                            source="native",
                        ))
                except AttributeError:
                    pass

        text = "\n".join(text_parts).strip()

        return ActionResult(
            tool_calls=calls,
            text=text,
            raw_response=response if isinstance(response, dict) else {"content": str(content_blocks)},
            extraction_method="native" if calls else "none",
        )

    def format_tools(self, tool_definitions):
        """Convert OpenAI-format tool definitions to Anthropic format.

        Anthropic expects: [{"name": "...", "description": "...", "input_schema": {...}}]
        """
        anthropic_tools = []
        for td in tool_definitions:
            if isinstance(td, dict):
                fn = td.get("function", td)
                anthropic_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        return anthropic_tools
