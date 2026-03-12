"""
Memory control and workflow tool registrations.

Registers: memory_control, run_workflow
"""

import logging

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Module-level references set by brain.py at startup
_memory_store = None
_preferences = None
_workflow_registry = None


def set_memory_refs(memory_store, preferences):
    """Set memory references for tool handlers. Called by assistant_loop."""
    global _memory_store, _preferences
    _memory_store = memory_store
    _preferences = preferences


def set_workflow_registry(registry):
    """Set workflow registry reference. Called by assistant_loop."""
    global _workflow_registry
    _workflow_registry = registry


# ===================================================================
# Handler functions
# ===================================================================

def _handle_memory_control(arguments):
    from features.memory.controls import handle_memory_command
    if _memory_store is None:
        return "Memory system not initialized."
    action = arguments.get("action", "recall")
    data = arguments.get("data", "")
    return handle_memory_command(action, data, _memory_store, _preferences)


def _handle_run_workflow(arguments, action_registry=None):
    from features.workflows.executor import execute_workflow
    if _workflow_registry is None:
        return "Workflow system not initialized."

    name = arguments.get("name", "")
    if not name:
        # List available workflows
        available = _workflow_registry.list_all()
        if not available:
            return "No workflows defined."
        lines = [f"  - {n}: {d}" for n, d in available.items()]
        return "Available workflows:\n" + "\n".join(lines)

    action = arguments.get("action", "run")

    if action == "run":
        # Import executor-level references
        from brain import _tool_executor
        return execute_workflow(
            name, _workflow_registry, _tool_executor, action_registry,
        )
    elif action == "list":
        available = _workflow_registry.list_all()
        lines = [f"  - {n}: {d}" for n, d in available.items()]
        return "Available workflows:\n" + "\n".join(lines)
    elif action == "create":
        steps_raw = arguments.get("steps", [])
        description = arguments.get("description", "")
        if not steps_raw:
            return "No steps provided for the workflow."
        _workflow_registry.register(name, steps_raw, description)
        return f"Workflow '{name}' created with {len(steps_raw)} steps."
    elif action == "delete":
        ok, msg = _workflow_registry.delete(name)
        return msg
    else:
        return f"Unknown workflow action: {action}"


# ===================================================================
# Registration
# ===================================================================

def register_memory_workflow_tools(registry: ToolRegistry):
    """Register memory control and workflow tools."""

    registry.register(ToolSpec(
        name="memory_control",
        description=(
            "Control the assistant's memory. Use for: 'remember that my favorite color is blue', "
            "'forget my email', 'what do you remember about me', 'private mode on/off', "
            "'show preferences'. ALWAYS use this for remember/forget/recall requests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["remember", "forget", "recall", "search",
                             "private_on", "private_off", "preferences"],
                    "description": "What to do: remember a fact, forget something, recall memories, search, toggle private mode, or manage preferences",
                },
                "data": {
                    "type": "string",
                    "description": "The fact to remember, what to forget, or search query. Examples: 'my favorite color is blue', 'email address', 'response_style: concise'",
                },
            },
            "required": ["action"]
        },
        handler=_handle_memory_control,
        aliases=["remember", "forget", "recall", "memories",
                 "remember_this", "forget_this", "private_mode", "my_preferences"],
        arg_aliases={"fact": "data", "memory": "data", "query": "data",
                     "text": "data", "what": "data", "key": "data"},
        primary_arg="action",
        core=False,  # Cloud-only: complex, rarely used in daily tasks
    ))

    registry.register(ToolSpec(
        name="run_workflow",
        description=(
            "Run a named workflow (multi-step routine). Built-in workflows: 'start my workday', "
            "'meeting mode', 'coding setup', 'end my day'. Use for: 'start my workday', "
            "'run meeting mode', 'list workflows'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workflow name, e.g. 'start my workday', 'meeting mode'",
                },
                "action": {
                    "type": "string",
                    "enum": ["run", "list", "create", "delete"],
                    "description": "What to do (default: run)",
                },
                "steps": {
                    "type": "array",
                    "description": "Steps for creating a workflow: [{tool, args}, ...]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                    },
                },
                "description": {
                    "type": "string",
                    "description": "Description for a new workflow",
                },
            },
            "required": ["name"]
        },
        handler=_handle_run_workflow,
        requires_registry=True,
        aliases=["workflow", "routine", "workday", "meeting_mode"],
        arg_aliases={"workflow": "name", "routine": "name"},
        primary_arg="name",
        core=False,  # Cloud-only: complex multi-step tool confuses 7B model
    ))

    logger.info("Registered 2 memory/workflow tools")
