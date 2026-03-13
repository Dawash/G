"""
Tool Contract System — typed ABI for all tools.

Every tool declares:
  - input_schema: dict (JSON Schema for arguments)
  - output_schema: dict (JSON Schema for return value)
  - side_effect_level: "none" | "local" | "system" | "network" | "destructive"
  - timeout: int (seconds)
  - retry_policy: {"max_retries": int, "backoff": float}
  - idempotent: bool
  - requires_confirmation: bool
  - allowed_tiers: list[ExecutionTier]
  - rollback_handler: Optional[Callable]

Contracts are the enforcement layer between the LLM's tool choice
and actual execution. validate_call() catches bad arguments BEFORE
the handler runs — no more crashes from hallucinated args.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Import execution tiers — graceful fallback if not available
try:
    from core.execution_tiers import ExecutionTier
except ImportError:
    # Stub so contracts work standalone
    from enum import IntEnum

    class ExecutionTier(IntEnum):
        DETERMINISTIC = 0
        STRUCTURED = 1
        ADAPTIVE = 2
        HUMAN_REQUIRED = 3


# ===================================================================
# Side-effect levels (ordered by severity)
# ===================================================================

SIDE_EFFECT_NONE = "none"             # Pure read, no state change
SIDE_EFFECT_LOCAL = "local"           # Local state only (reminders, memory)
SIDE_EFFECT_SYSTEM = "system"         # OS-level changes (apps, windows, files)
SIDE_EFFECT_NETWORK = "network"       # Network calls (web, email, search)
SIDE_EFFECT_DESTRUCTIVE = "destructive"  # Hard to reverse (delete, uninstall)

_SIDE_EFFECT_ORDER = [
    SIDE_EFFECT_NONE, SIDE_EFFECT_LOCAL, SIDE_EFFECT_SYSTEM,
    SIDE_EFFECT_NETWORK, SIDE_EFFECT_DESTRUCTIVE,
]


# ===================================================================
# Contract dataclass
# ===================================================================

@dataclass
class ToolContract:
    """Full behavioral contract for a single tool.

    Attributes:
        name: Canonical tool name.
        input_schema: JSON Schema dict for validating arguments.
        output_schema: JSON Schema dict for return value (informational).
        side_effect_level: How much state this tool can change.
        timeout: Max execution time in seconds.
        retry_policy: Dict with max_retries and backoff multiplier.
        idempotent: Safe to call multiple times with same args.
        requires_confirmation: Must ask user before executing.
        allowed_tiers: Execution tiers where this tool may run.
        rollback_handler: Optional callable(args) -> str for undo.
    """
    name: str
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    side_effect_level: str = SIDE_EFFECT_NONE
    timeout: int = 30
    retry_policy: dict = field(default_factory=lambda: {"max_retries": 0, "backoff": 1.0})
    idempotent: bool = True
    requires_confirmation: bool = False
    allowed_tiers: list = field(default_factory=lambda: [
        ExecutionTier.DETERMINISTIC, ExecutionTier.STRUCTURED,
        ExecutionTier.ADAPTIVE,
    ])
    rollback_handler: Optional[Callable] = None


# ===================================================================
# Contract registry
# ===================================================================

class ContractRegistry:
    """Stores and looks up ToolContracts by name."""

    def __init__(self):
        self._contracts: dict[str, ToolContract] = {}

    def register(self, contract: ToolContract) -> None:
        """Register a tool contract."""
        self._contracts[contract.name] = contract
        logger.debug(f"Contract registered: {contract.name}")

    def get_contract(self, tool_name: str) -> Optional[ToolContract]:
        """Look up a tool's contract. Returns None if not registered."""
        return self._contracts.get(tool_name)

    def has(self, tool_name: str) -> bool:
        return tool_name in self._contracts

    def all_names(self) -> list[str]:
        return list(self._contracts.keys())

    def validate_call(self, tool_name: str,
                      args: Optional[dict] = None) -> tuple[bool, list[str]]:
        """Validate tool arguments against the contract's input_schema.

        Checks:
          - Required fields are present
          - Field types match (string, number, boolean, array)
          - Enum values are valid

        Returns:
            (valid, errors): valid is True if args pass validation,
            errors is a list of human-readable error strings.
        """
        args = args or {}
        contract = self._contracts.get(tool_name)
        if contract is None:
            # No contract = no validation (permissive for dynamic tools)
            return True, []

        errors = []
        schema = contract.input_schema
        if not schema:
            return True, []

        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        # Check required fields
        for req_field in required:
            if req_field not in args:
                errors.append(f"Missing required argument: '{req_field}'")
            elif args[req_field] is None or (isinstance(args[req_field], str)
                                              and not args[req_field].strip()):
                errors.append(f"Required argument '{req_field}' is empty")

        # Check types and enum constraints
        for arg_name, arg_value in args.items():
            if arg_name not in properties:
                continue  # Extra args are ignored (LLMs often add extras)

            prop = properties[arg_name]
            expected_type = prop.get("type")

            # Type check
            if expected_type and arg_value is not None:
                if not _type_matches(arg_value, expected_type):
                    errors.append(
                        f"Argument '{arg_name}': expected {expected_type}, "
                        f"got {type(arg_value).__name__}"
                    )

            # Enum check
            if "enum" in prop and arg_value is not None:
                allowed = prop["enum"]
                if arg_value not in allowed:
                    errors.append(
                        f"Argument '{arg_name}': '{arg_value}' not in "
                        f"allowed values {allowed}"
                    )

        return len(errors) == 0, errors

    def get_side_effect_level(self, tool_name: str) -> str:
        """Get the side-effect level for a tool. Defaults to 'system'."""
        contract = self._contracts.get(tool_name)
        if contract:
            return contract.side_effect_level
        return SIDE_EFFECT_SYSTEM

    def get_timeout(self, tool_name: str) -> int:
        """Get timeout in seconds for a tool. Defaults to 30."""
        contract = self._contracts.get(tool_name)
        if contract:
            return contract.timeout
        return 30

    def requires_confirmation(self, tool_name: str) -> bool:
        """Check if a tool requires user confirmation."""
        contract = self._contracts.get(tool_name)
        if contract:
            return contract.requires_confirmation
        return False

    def summary(self) -> dict:
        """Return a summary of all registered contracts."""
        result = {}
        for name, c in self._contracts.items():
            result[name] = {
                "side_effect": c.side_effect_level,
                "timeout": c.timeout,
                "idempotent": c.idempotent,
                "requires_confirmation": c.requires_confirmation,
                "tiers": [t.name for t in c.allowed_tiers],
            }
        return result


def _type_matches(value, expected_type: str) -> bool:
    """Check if a Python value matches a JSON Schema type."""
    if expected_type == "string":
        return isinstance(value, str)
    elif expected_type == "number":
        return isinstance(value, (int, float))
    elif expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    elif expected_type == "boolean":
        return isinstance(value, bool)
    elif expected_type == "array":
        return isinstance(value, (list, tuple))
    elif expected_type == "object":
        return isinstance(value, dict)
    return True  # Unknown type = pass


# ===================================================================
# Pre-populated contracts for core tools
# ===================================================================

def _build_default_contracts() -> ContractRegistry:
    """Build and return a ContractRegistry pre-populated with
    contracts for the most important tools."""

    reg = ContractRegistry()

    # --- Tier 0: DETERMINISTIC, no side effects ---

    reg.register(ToolContract(
        name="get_time",
        input_schema={
            "type": "object",
            "properties": {},
        },
        output_schema={"type": "object", "properties": {"time": {"type": "string"}}},
        side_effect_level=SIDE_EFFECT_NONE,
        timeout=5,
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="get_weather",
        input_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name (optional)"},
            },
        },
        output_schema={"type": "object", "properties": {"weather": {"type": "string"}}},
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=10,
        retry_policy={"max_retries": 2, "backoff": 1.0},
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="get_forecast",
        input_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name (optional)"},
            },
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=10,
        retry_policy={"max_retries": 2, "backoff": 1.0},
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="get_news",
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["general", "tech", "sports", "entertainment",
                             "science", "business", "health"],
                },
                "query": {"type": "string"},
                "country": {"type": "string"},
            },
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=15,
        retry_policy={"max_retries": 1, "backoff": 2.0},
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="set_reminder",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Reminder text"},
                "time": {"type": "string", "description": "When to remind (NLP parsed)"},
            },
            "required": ["message", "time"],
        },
        side_effect_level=SIDE_EFFECT_LOCAL,
        timeout=5,
        idempotent=False,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="list_reminders",
        input_schema={"type": "object", "properties": {}},
        side_effect_level=SIDE_EFFECT_NONE,
        timeout=5,
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="google_search",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=10,
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="web_read",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to read"},
            },
            "required": ["url"],
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=30,
        retry_policy={"max_retries": 2, "backoff": 2.0},
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="web_search_answer",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=30,
        retry_policy={"max_retries": 1, "backoff": 2.0},
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    reg.register(ToolContract(
        name="read_clipboard",
        input_schema={"type": "object", "properties": {}},
        side_effect_level=SIDE_EFFECT_NONE,
        timeout=5,
        idempotent=True,
        allowed_tiers=[ExecutionTier.DETERMINISTIC],
    ))

    # --- Tier 1: STRUCTURED_AUTOMATION ---

    reg.register(ToolContract(
        name="open_app",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name"},
            },
            "required": ["name"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=15,
        idempotent=True,
        requires_confirmation=False,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="close_app",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name"},
            },
            "required": ["name"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=10,
        idempotent=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="minimize_app",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name"},
            },
            "required": ["name"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=5,
        idempotent=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="browser_action",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "click", "fill", "read", "screenshot",
                             "get_url", "get_tabs", "switch_tab", "new_tab",
                             "close_tab", "back", "forward", "find_text",
                             "run_js", "wait_for"],
                },
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "tab_id": {"type": "number"},
            },
            "required": ["action"],
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=30,
        retry_policy={"max_retries": 1, "backoff": 1.0},
        idempotent=False,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="send_email",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body"},
            },
            "required": ["to", "subject", "body"],
        },
        side_effect_level=SIDE_EFFECT_NETWORK,
        timeout=30,
        idempotent=False,
        requires_confirmation=True,
        allowed_tiers=[ExecutionTier.STRUCTURED],
    ))

    reg.register(ToolContract(
        name="create_file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File contents"},
            },
            "required": ["path"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=15,
        idempotent=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="play_music",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "play_query", "pause", "resume",
                             "next", "previous", "volume_up", "volume_down"],
                },
                "query": {"type": "string"},
                "app": {"type": "string", "enum": ["spotify", "youtube"]},
            },
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=20,
        idempotent=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="system_command",
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["shutdown", "restart", "sleep", "cancel_shutdown"],
                },
            },
            "required": ["command"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=10,
        idempotent=False,
        requires_confirmation=True,
        allowed_tiers=[ExecutionTier.STRUCTURED],
    ))

    # --- Tier 2: ADAPTIVE_AUTOMATION ---

    reg.register(ToolContract(
        name="click_at",
        input_schema={
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate"},
                "y": {"type": "number", "description": "Y coordinate"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer"},
            },
            "required": ["x", "y"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=5,
        idempotent=False,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="type_text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=10,
        idempotent=False,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="press_key",
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key or combo (e.g. 'ctrl+c')"},
            },
            "required": ["key"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=5,
        idempotent=False,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="scroll",
        input_schema={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer"},
            },
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=5,
        idempotent=True,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="take_screenshot",
        input_schema={"type": "object", "properties": {}},
        side_effect_level=SIDE_EFFECT_NONE,
        timeout=10,
        idempotent=True,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="find_on_screen",
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to find"},
            },
            "required": ["description"],
        },
        side_effect_level=SIDE_EFFECT_NONE,
        timeout=15,
        idempotent=True,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="run_terminal",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell command"},
                "admin": {"type": "boolean", "description": "Run as admin"},
            },
            "required": ["command"],
        },
        side_effect_level=SIDE_EFFECT_DESTRUCTIVE,
        timeout=60,
        retry_policy={"max_retries": 0, "backoff": 1.0},
        idempotent=False,
        requires_confirmation=False,  # safety_policy handles per-command confirmation
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="manage_files",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "move", "copy", "rename", "delete",
                             "zip", "unzip", "read", "info"],
                },
                "path": {"type": "string", "description": "File or directory path"},
                "destination": {"type": "string", "description": "Destination path"},
            },
            "required": ["action", "path"],
        },
        side_effect_level=SIDE_EFFECT_DESTRUCTIVE,
        timeout=30,
        idempotent=False,
        requires_confirmation=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="manage_software",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "install", "uninstall", "update", "update_all", "list"],
                },
                "name": {"type": "string", "description": "Software name"},
            },
            "required": ["action"],
        },
        side_effect_level=SIDE_EFFECT_DESTRUCTIVE,
        timeout=120,
        retry_policy={"max_retries": 1, "backoff": 5.0},
        idempotent=False,
        requires_confirmation=True,
        allowed_tiers=[ExecutionTier.STRUCTURED, ExecutionTier.ADAPTIVE],
    ))

    reg.register(ToolContract(
        name="agent_task",
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "High-level goal"},
            },
            "required": ["goal"],
        },
        side_effect_level=SIDE_EFFECT_SYSTEM,
        timeout=300,
        idempotent=False,
        requires_confirmation=True,
        allowed_tiers=[ExecutionTier.ADAPTIVE],
    ))

    return reg


# ===================================================================
# Module-level default registry (lazy init)
# ===================================================================

_default_contracts: Optional[ContractRegistry] = None


def get_default_contracts() -> ContractRegistry:
    """Get or create the default contract registry."""
    global _default_contracts
    if _default_contracts is None:
        _default_contracts = _build_default_contracts()
    return _default_contracts


def validate_call(tool_name: str, args: Optional[dict] = None) -> tuple[bool, list[str]]:
    """Convenience function: validate a tool call against its contract."""
    return get_default_contracts().validate_call(tool_name, args)


def get_contract(tool_name: str) -> Optional[ToolContract]:
    """Convenience function: look up a tool's contract."""
    return get_default_contracts().get_contract(tool_name)
