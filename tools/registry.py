"""
Tool registry — single source of truth for all tools.

Central registry of ToolSpecs. Provides:
  - Tool lookup by canonical name or alias
  - LLM schema generation (replaces brain_defs.build_tool_definitions)
  - Argument normalization (replaces brain_defs._normalize_tool_args)
  - Primary argument resolution (replaces brain_defs._guess_primary_arg)

Module-level default registry accessible via get_default() / set_default().
"""

import logging
from typing import Optional

from tools.schemas import ToolSpec

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of all available tools and their metadata."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._alias_map: dict[str, str] = {}  # alias → canonical name

    def register(self, spec: ToolSpec):
        """Register a tool spec. Overwrites if name already exists."""
        self._tools[spec.name] = spec
        # Build alias mappings
        for alias in spec.aliases:
            key = alias.lower().strip()
            if key and key != spec.name:
                self._alias_map[key] = spec.name
        logger.debug(f"Registered tool: {spec.name}")

    def unregister(self, name: str):
        """Remove a tool by name."""
        spec = self._tools.pop(name, None)
        if spec:
            # Clean up alias map
            for alias in spec.aliases:
                self._alias_map.pop(alias.lower().strip(), None)

    def get(self, name: str) -> Optional[ToolSpec]:
        """Look up a tool by canonical name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def all_names(self) -> list[str]:
        return list(self._tools.keys())

    def all_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def core_names(self) -> list[str]:
        """Return names of tools marked as core (for local models)."""
        return [s.name for s in self._tools.values() if s.core]

    # -----------------------------------------------------------------
    # Name resolution — replaces brain_defs._resolve_tool_name
    # -----------------------------------------------------------------

    def resolve_name(self, raw_name: str) -> Optional[str]:
        """Resolve an alias or abbreviated name to canonical tool name.

        Checks: canonical name → alias map → underscore-stripped match.
        """
        if not raw_name or not isinstance(raw_name, str):
            return None
        name = raw_name.strip().lower()
        # Direct canonical match
        if name in self._tools:
            return name
        # Alias match
        if name in self._alias_map:
            return self._alias_map[name]
        # Underscore-stripped match
        no_under = name.replace("_", "")
        for real in self._tools:
            if real.replace("_", "") == no_under:
                return real
        return None

    # -----------------------------------------------------------------
    # Argument normalization — replaces brain_defs._normalize_tool_args
    # -----------------------------------------------------------------

    def normalize_args(self, tool_name: str, args: dict) -> dict:
        """Fix argument name mismatches using tool's arg_aliases."""
        if not args:
            return args
        spec = self._tools.get(tool_name)
        if not spec or not spec.arg_aliases:
            return args
        return {spec.arg_aliases.get(k, k): v for k, v in args.items()}

    def get_primary_arg(self, tool_name: str) -> str:
        """Get the primary argument name for positional arg handling."""
        spec = self._tools.get(tool_name)
        return spec.primary_arg if spec else "name"

    # -----------------------------------------------------------------
    # LLM schema generation — replaces brain_defs.build_tool_definitions
    # -----------------------------------------------------------------

    def build_llm_schemas(self, core_only: bool = False) -> list[dict]:
        """Generate OpenAI-format tool definitions from registry.

        Args:
            core_only: If True, only return tools marked as core (for Ollama).
        """
        schemas = []
        for spec in self._tools.values():
            if not spec.llm_enabled:
                continue
            if core_only and not spec.core:
                continue
            schemas.append(spec.to_openai_schema())
        return schemas

    def to_prompt_text(self, core_only: bool = False) -> str:
        """Convert tool definitions to plain-text for system prompt embedding."""
        lines = []
        for spec in self._tools.values():
            if not spec.llm_enabled:
                continue
            if core_only and not spec.core:
                continue
            params = spec.parameters.get("properties", {})
            required = spec.parameters.get("required", [])
            param_parts = []
            for pname, pinfo in params.items():
                req = " (required)" if pname in required else " (optional)"
                param_parts.append(f'    - {pname}: {pinfo.get("description", "")}{req}')
            param_str = "\n".join(param_parts) if param_parts else "    (no parameters)"
            lines.append(f"  {spec.name}: {spec.description}\n{param_str}")
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Legacy compatibility
    # -----------------------------------------------------------------

    def to_openai_schemas(self, names: list[str] = None) -> list[dict]:
        """Convert registered tools to OpenAI function-calling format.

        Args:
            names: Optional subset of tool names. If None, returns all.
        """
        specs = self._tools.values() if names is None else [
            self._tools[n] for n in names if n in self._tools
        ]
        return [s.to_openai_schema() for s in specs]


# ===================================================================
# Module-level default registry (singleton)
# ===================================================================

_default_registry: Optional[ToolRegistry] = None


def get_default() -> Optional[ToolRegistry]:
    """Get the global default tool registry."""
    return _default_registry


def set_default(registry: ToolRegistry):
    """Set the global default tool registry."""
    global _default_registry
    _default_registry = registry
