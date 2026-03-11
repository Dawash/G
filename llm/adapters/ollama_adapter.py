"""
Ollama adapter — extracts tool calls from Ollama's native + JSON fallback responses.

Ollama returns tool calls in two ways:
  1. Native: response["message"]["tool_calls"] (when model supports it)
  2. JSON in text: model outputs ```json blocks with tool invocations
  3. Prompt-based: model outputs action descriptions we parse

This adapter handles all three tiers.
"""

import json
import logging
import re

from llm.adapters.base import ProviderAdapter, ActionResult, ToolCall

logger = logging.getLogger(__name__)


class OllamaAdapter(ProviderAdapter):
    """Adapter for Ollama API responses."""

    provider_name = "ollama"

    def extract(self, response, tool_definitions=None):
        """Extract tool calls from Ollama response.

        Tries: native tool_calls → JSON in content → prompt parsing.
        """
        if not response:
            return ActionResult()

        message = response.get("message", {})
        content = message.get("content", "")

        # --- Tier 1: Native tool calls ---
        tool_calls_raw = message.get("tool_calls", [])
        if tool_calls_raw:
            calls = []
            for tc in tool_calls_raw:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name:
                    calls.append(ToolCall(
                        name=name,
                        arguments=args,
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

        # --- Tier 2: JSON extraction from content ---
        if content:
            calls = self._extract_json_tools(content)
            if calls:
                # Remove JSON block from text
                text = re.sub(r'```json\s*\{.*?\}\s*```', '', content,
                             flags=re.DOTALL).strip()
                text = re.sub(r'\{["\']?actions?["\']?\s*:\s*\[.*?\]\}', '', text,
                             flags=re.DOTALL).strip()
                return ActionResult(
                    tool_calls=calls,
                    text=text,
                    raw_response=response,
                    extraction_method="json",
                )

        # --- Tier 3: No tool calls found ---
        return ActionResult(
            text=content,
            raw_response=response,
            extraction_method="none",
        )

    def _extract_json_tools(self, content):
        """Extract tool calls from JSON blocks in LLM text output."""
        calls = []

        # Try ```json blocks
        json_blocks = re.findall(r'```json\s*(\{.*?\})\s*```', content, re.DOTALL)
        for block in json_blocks:
            parsed = self._parse_tool_json(block)
            if parsed:
                calls.extend(parsed)

        if calls:
            return calls

        # Try raw JSON objects with "actions" key
        action_blocks = re.findall(r'\{["\']?actions?["\']?\s*:\s*\[.*?\]\}',
                                   content, re.DOTALL)
        for block in action_blocks:
            parsed = self._parse_tool_json(block)
            if parsed:
                calls.extend(parsed)

        if calls:
            return calls

        # Try single tool call object: {"tool": "...", "args": {...}}
        single_blocks = re.findall(r'\{\s*["\']tool["\']\s*:\s*["\'](\w+)["\'].*?\}',
                                   content, re.DOTALL)
        for match in single_blocks:
            # Re-extract full object
            pattern = r'\{[^{}]*["\']tool["\']\s*:\s*["\']' + re.escape(match) + r'["\'][^{}]*\}'
            full = re.search(pattern, content, re.DOTALL)
            if full:
                parsed = self._parse_tool_json(full.group())
                if parsed:
                    calls.extend(parsed)

        return calls

    def _parse_tool_json(self, text):
        """Parse a JSON string into ToolCall objects."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try fixing common issues
            text = text.replace("'", '"')
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return []

        calls = []

        # Format: {"actions": [{"tool": "...", "args": {...}}]}
        if isinstance(data, dict) and "actions" in data:
            for action in data["actions"]:
                if isinstance(action, dict) and "tool" in action:
                    calls.append(ToolCall(
                        name=action["tool"],
                        arguments=action.get("args", action.get("arguments", {})),
                        confidence=0.85,
                        source="json",
                    ))

        # Format: {"tool": "...", "args": {...}}
        elif isinstance(data, dict) and "tool" in data:
            calls.append(ToolCall(
                name=data["tool"],
                arguments=data.get("args", data.get("arguments", {})),
                confidence=0.85,
                source="json",
            ))

        # Format: {"name": "...", "arguments": {...}}
        elif isinstance(data, dict) and "name" in data:
            calls.append(ToolCall(
                name=data["name"],
                arguments=data.get("arguments", data.get("args", {})),
                confidence=0.8,
                source="json",
            ))

        return calls
