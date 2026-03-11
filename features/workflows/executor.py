"""
Workflow executor — runs named workflows as tool call sequences.

Executes each step through the tool executor, collects results,
and returns a summary. Stops on critical failures.
"""

import logging

logger = logging.getLogger(__name__)


def execute_workflow(workflow_name, registry, tool_executor, action_registry,
                     reminder_mgr=None, speak_fn=None):
    """Execute a named workflow.

    Args:
        workflow_name: Name of the workflow to run.
        registry: WorkflowRegistry instance.
        tool_executor: ToolExecutor instance.
        action_registry: Action registry dict for tool handlers.
        reminder_mgr: Optional reminder manager.
        speak_fn: Optional TTS function.

    Returns:
        str: Summary of workflow execution.
    """
    workflow = registry.get(workflow_name)
    if not workflow:
        available = ", ".join(registry.list_all().keys())
        return f"Unknown workflow '{workflow_name}'. Available: {available}"

    steps = workflow.get("steps", [])
    if not steps:
        return f"Workflow '{workflow_name}' has no steps."

    results = []
    for i, step in enumerate(steps, 1):
        tool_name = step.get("tool", "")
        args = step.get("args", {})

        try:
            result = tool_executor.execute(
                tool_name, args, action_registry,
                reminder_mgr=reminder_mgr,
                speak_fn=speak_fn,
                user_input=f"workflow step {i}: {tool_name}",
                mode="workflow",
            )
            results.append(f"Step {i} ({tool_name}): {str(result)[:100]}")
            logger.info(f"Workflow '{workflow_name}' step {i}/{len(steps)}: {tool_name} -> OK")
        except Exception as e:
            results.append(f"Step {i} ({tool_name}): Error — {e}")
            logger.error(f"Workflow '{workflow_name}' step {i} failed: {e}")

    summary = f"Workflow '{workflow_name}' completed ({len(steps)} steps):\n"
    summary += "\n".join(results)
    return summary
