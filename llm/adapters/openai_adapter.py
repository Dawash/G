"""
OpenAI adapter — extracts tool calls from OpenAI/OpenRouter API responses.

OpenAI returns tool calls in the response message:
  response.choices[0].message.tool_calls = [
      {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
  ]

Also handles the legacy function_call format.
"""

import json
import logging

from llm.adapters.base import ProviderAdapter, ActionResult, ToolCall

logger = logging.getLogger(__name__)


class OpenAIAdapter(ProviderAdapter):
    """Adapter for OpenAI and OpenRouter API responses."""

    provider_name = "openai"

    def extract(self, response, tool_definitions=None):
        """Extract tool calls from OpenAI response.

        Handles both dict responses and ChatCompletion objects.
        """
        if not response:
            return ActionResult()

        # Handle dict format
        if isinstance(response, dict):
            return self._extract_from_dict(response)

        # Handle ChatCompletion object (openai library)
        try:
            message = response.choices[0].message
            content = message.content or ""

            # Check for tool_calls
            if hasattr(message, "tool_calls") and message.tool_calls:
                calls = []
                for tc in message.tool_calls:
                    fn = tc.function
                    name = fn.name
                    try:
                        args = json.loads(fn.arguments) if isinstance(fn.arguments, str) else fn.arguments
                    except json.JSONDecodeError:
                        args = {}
                    calls.append(ToolCall(
                        name=name,
                        arguments=args or {},
                        confidence=1.0,
                        source="native",
                    ))
                return ActionResult(
                    tool_calls=calls,
                    text=content,
                    raw_response={"content": content},
                    extraction_method="native",
                )

            # Check legacy function_call
            if hasattr(message, "function_call") and message.function_call:
                fc = message.function_call
                try:
                    args = json.loads(fc.arguments) if isinstance(fc.arguments, str) else fc.arguments
                except json.JSONDecodeError:
                    args = {}
                return ActionResult(
                    tool_calls=[ToolCall(
                        name=fc.name,
                        arguments=args or {},
                        confidence=1.0,
                        source="native",
                    )],
                    text=content,
                    raw_response={"content": content},
                    extraction_method="native",
                )

            return ActionResult(
                text=content,
                raw_response={"content": content},
                extraction_method="none",
            )

        except (AttributeError, IndexError) as e:
            logger.debug(f"OpenAI extract error: {e}")
            return ActionResult()

    def _extract_from_dict(self, response):
        """Extract from dict-format response."""
        choices = response.get("choices", [])
        if not choices:
            return ActionResult(raw_response=response)

        message = choices[0].get("message", {})
        content = message.get("content", "")

        # Native tool_calls
        tool_calls_raw = message.get("tool_calls", [])
        if tool_calls_raw:
            calls = []
            for tc in tool_calls_raw:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                if name:
                    calls.append(ToolCall(
                        name=name,
                        arguments=args or {},
                        confidence=1.0,
                        source="native",
                    ))
            if calls:
                return ActionResult(
                    tool_calls=calls,
                    text=content,
                    raw_response=response,
                    extraction_method="native",
                )

        # Legacy function_call
        fc = message.get("function_call")
        if fc:
            try:
                args = json.loads(fc.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            return ActionResult(
                tool_calls=[ToolCall(
                    name=fc.get("name", ""),
                    arguments=args,
                    confidence=1.0,
                    source="native",
                )],
                text=content,
                raw_response=response,
                extraction_method="native",
            )

        return ActionResult(
            text=content,
            raw_response=response,
            extraction_method="none",
        )
