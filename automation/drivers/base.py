"""
Base class for app-specific drivers.

Each driver provides structured knowledge about an application:
  - What controls/elements exist and how to find them
  - Common operations and their implementations
  - Keyboard shortcuts
  - Preconditions and postconditions for actions
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AppAction:
    """A known action for an app driver."""
    name: str
    description: str
    steps: list = field(default_factory=list)  # List of (tool, args) tuples
    shortcuts: list = field(default_factory=list)  # Keyboard shortcuts
    preconditions: list = field(default_factory=list)  # What must be true
    postconditions: list = field(default_factory=list)  # How to verify


class AppDriver:
    """Base class for app-specific drivers.

    Subclasses should:
      1. Set `app_name`, `process_names`, `window_patterns`
      2. Register actions in __init__ via `_register_action()`
      3. Implement `detect()` to check if this driver's app is active
    """

    app_name: str = ""
    process_names: list = []
    window_patterns: list = []

    def __init__(self):
        self.actions: dict[str, AppAction] = {}
        self.shortcuts: dict[str, str] = {}

    def _register_action(self, action: AppAction):
        """Register an action this driver can handle."""
        self.actions[action.name] = action

    def _register_shortcut(self, name, keys):
        """Register a keyboard shortcut."""
        self.shortcuts[name] = keys

    def detect(self, window_title="", process_name=""):
        """Check if this driver applies to the given window/process."""
        title_lower = window_title.lower()
        proc_lower = process_name.lower()

        for pattern in self.window_patterns:
            if pattern.lower() in title_lower:
                return True
        for pname in self.process_names:
            if pname.lower() in proc_lower:
                return True
        return False

    def get_action(self, action_name):
        """Get a registered action by name."""
        return self.actions.get(action_name)

    def list_actions(self):
        """List all available actions."""
        return list(self.actions.keys())

    def execute_action(self, action_name, **kwargs):
        """Execute a registered action.

        Override in subclasses for custom execution logic.
        """
        action = self.actions.get(action_name)
        if not action:
            return f"Unknown action: {action_name}"

        # Default: execute steps sequentially
        results = []
        for tool_name, tool_args in action.steps:
            try:
                result = self._execute_tool(tool_name, tool_args, **kwargs)
                results.append(result)
            except Exception as e:
                return f"Action '{action_name}' failed at step {tool_name}: {e}"

        return " → ".join(str(r) for r in results) if results else "Action completed."

    def _execute_tool(self, tool_name, args, **kwargs):
        """Execute a single tool call. Override for custom dispatch."""
        if tool_name == "press_key":
            from computer import press_key
            return press_key(args.get("keys", ""))
        elif tool_name == "focus_window":
            from automation.ui_control import focus_window
            return focus_window(args.get("name", self.app_name))
        elif tool_name == "click_control":
            from automation.ui_control import click_control
            return click_control(**args)
        elif tool_name == "set_control_text":
            from automation.ui_control import set_control_text
            return set_control_text(**args)
        elif tool_name == "wait":
            import time
            time.sleep(args.get("seconds", 1))
            return "waited"
        return f"Unknown tool: {tool_name}"

    def get_shortcut(self, operation):
        """Get keyboard shortcut for an operation."""
        return self.shortcuts.get(operation)


# ===================================================================
# Driver registry
# ===================================================================

_drivers: list[AppDriver] = []


def register_driver(driver: AppDriver):
    """Register an app driver."""
    _drivers.append(driver)


def get_driver_for(window_title="", process_name=""):
    """Find the right driver for the current context."""
    for driver in _drivers:
        if driver.detect(window_title, process_name):
            return driver
    return None


def get_driver_by_name(app_name):
    """Find a driver by app name."""
    name_lower = app_name.lower()
    for driver in _drivers:
        if driver.app_name.lower() == name_lower:
            return driver
    return None


def list_drivers():
    """List all registered drivers."""
    return [d.app_name for d in _drivers]
