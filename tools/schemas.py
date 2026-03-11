"""
ToolSpec — standard metadata type for registered tools.

Each tool declares its name, schema, handler, safety level,
verification, and rollback support in one place.

ToolSpec is the SINGLE SOURCE OF TRUTH for:
  - LLM tool schemas (generated via to_openai_schema)
  - Tool name aliases (resolved via ToolRegistry.resolve_name)
  - Argument aliases (normalized via ToolRegistry.normalize_args)
  - Core/full tool set membership
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """Full metadata for a single tool.

    Attributes:
        name: Canonical tool name (e.g. "open_app").
        description: Human-readable description for LLM schema.
                     Should include routing hints ("Use for: ...", "NEVER use for: ...").
        parameters: OpenAI-format parameter schema dict.
        handler: Callable(arguments, context) -> str.  The actual work.
        safety: "safe" | "moderate" | "sensitive" | "critical".
        confirm_condition: Optional callable(arguments) -> str|None.
        verifier: Optional callable(arguments, result, user_input) -> (bool, list, list).
        rollback: Optional callable(arguments, action_registry) -> str.
        rollback_description: Template for undo stack description (e.g. "opened {name}").
        cacheable: Whether results can be cached.
        cache_ttl: TTL in seconds (only if cacheable).
        requires_registry: Tool handler needs action_registry injected.
        requires_reminder_mgr: Tool handler needs reminder_mgr injected.
        requires_speak_fn: Tool handler needs speak_fn injected.
        aliases: Alternative names the LLM might use (e.g. ["launch", "start"] for open_app).
        arg_aliases: Map of wrong_arg_name → correct_arg_name for fixing LLM output.
        primary_arg: The default arg name when LLM uses a positional/single value.
        core: Whether to include in the reduced tool set for local models (Ollama).
        llm_enabled: Whether to include in LLM tool schemas.
    """
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    handler: Optional[Callable] = None

    # Safety
    safety: str = "safe"
    confirm_condition: Optional[Callable] = None

    # Verification
    verifier: Optional[Callable] = None

    # Undo
    rollback: Optional[Callable] = None
    rollback_description: Optional[str] = None

    # Caching
    cacheable: bool = False
    cache_ttl: int = 0

    # Dependency flags
    requires_registry: bool = False
    requires_reminder_mgr: bool = False
    requires_speak_fn: bool = False
    requires_user_input: bool = False
    requires_quick_chat: bool = False

    # Routing metadata — single source of truth for aliases and LLM visibility
    aliases: list = field(default_factory=list)
    arg_aliases: dict = field(default_factory=dict)
    primary_arg: str = "name"
    core: bool = False
    llm_enabled: bool = True

    # Process isolation — run in subprocess for crash isolation
    isolate: bool = False

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }
