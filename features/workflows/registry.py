"""
Workflow registry — stores and retrieves named workflows.

Each workflow is a named sequence of tool calls that can be
executed as a single command. Persisted as JSON.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_WORKFLOWS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "workflows.json")

# Built-in workflows that ship with the assistant
_BUILTIN_WORKFLOWS = {
    "start my workday": {
        "description": "Morning routine: open browser, check weather, list reminders",
        "steps": [
            {"tool": "open_app", "args": {"name": "Chrome"}},
            {"tool": "get_weather", "args": {}},
            {"tool": "list_reminders", "args": {}},
        ],
    },
    "meeting mode": {
        "description": "Prepare for a meeting: open Teams, pause music",
        "steps": [
            {"tool": "open_app", "args": {"name": "Teams"}},
            {"tool": "play_music", "args": {"action": "pause"}},
        ],
    },
    "coding setup": {
        "description": "Open development tools",
        "steps": [
            {"tool": "open_app", "args": {"name": "VS Code"}},
            {"tool": "open_app", "args": {"name": "Terminal"}},
        ],
    },
    "end my day": {
        "description": "Wind down: check reminders, close apps",
        "steps": [
            {"tool": "list_reminders", "args": {}},
            {"tool": "get_forecast", "args": {}},
        ],
    },
}


class WorkflowRegistry:
    """Stores and retrieves named workflows."""

    def __init__(self, file_path=_WORKFLOWS_FILE):
        self._file_path = file_path
        self._workflows = dict(_BUILTIN_WORKFLOWS)
        self._load()

    def _load(self):
        """Load user workflows from disk."""
        if not os.path.exists(self._file_path):
            return
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                user_workflows = json.load(f)
            self._workflows.update(user_workflows)
        except Exception as e:
            logger.warning(f"Failed to load workflows: {e}")

    def _save(self):
        """Save user workflows (non-builtin) to disk."""
        user_only = {
            k: v for k, v in self._workflows.items()
            if k not in _BUILTIN_WORKFLOWS
        }
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(user_only, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save workflows: {e}")

    def register(self, name, steps, description=""):
        """Register or update a named workflow.

        Args:
            name: Workflow name (e.g. "start my workday").
            steps: List of {"tool": str, "args": dict} dicts.
            description: Human-readable description.
        """
        name = name.lower().strip()
        self._workflows[name] = {
            "description": description,
            "steps": steps,
        }
        self._save()
        logger.info(f"Workflow registered: '{name}' ({len(steps)} steps)")

    def get(self, name):
        """Get a workflow by name. Returns None if not found."""
        return self._workflows.get(name.lower().strip())

    def list_all(self):
        """List all available workflows with descriptions."""
        return {
            name: wf.get("description", "")
            for name, wf in sorted(self._workflows.items())
        }

    def delete(self, name):
        """Delete a user workflow. Cannot delete built-in workflows."""
        name = name.lower().strip()
        if name in _BUILTIN_WORKFLOWS:
            return False, "Cannot delete built-in workflow."
        if name not in self._workflows:
            return False, f"Workflow '{name}' not found."
        del self._workflows[name]
        self._save()
        return True, f"Workflow '{name}' deleted."

    def has(self, name):
        return name.lower().strip() in self._workflows
