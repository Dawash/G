"""
Base plugin class for G Assistant — Mycroft-inspired skill architecture.

Every plugin extends BasePlugin and can:
  - Register intent patterns (regex or keyword) that trigger the plugin
  - Register tool handlers that the LLM can call
  - Hook into lifecycle events (startup, shutdown, wake)
  - Access brain.quick_chat() for LLM responses
  - Access memory store for persistent data

Example plugin:

    class WeatherAlertPlugin(BasePlugin):
        name = "weather_alert"
        description = "Alerts user about severe weather"
        version = "1.0"

        def get_intents(self):
            return [
                PluginIntent(r"severe weather|storm warning|tornado", self.check_alerts),
            ]

        def check_alerts(self, text, match):
            return "No severe weather alerts in your area right now."
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PluginIntent:
    """An intent pattern that triggers a plugin handler.

    Args:
        pattern: Regex pattern to match user input (case-insensitive).
        handler: Function(text, match) -> str response.
        priority: Higher = checked first. Default 50. Built-in tools are 100.
        description: Human-readable description for debugging.
    """
    pattern: str
    handler: Callable
    priority: int = 50
    description: str = ""

    def __post_init__(self):
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def match(self, text):
        """Try to match the pattern against text. Returns re.Match or None."""
        return self._compiled.search(text)


@dataclass
class PluginTool:
    """A tool that the LLM can call, provided by a plugin.

    Args:
        name: Tool name (must be unique across all plugins).
        description: What the tool does (shown to LLM).
        parameters: JSON Schema for tool arguments.
        handler: Function(arguments: dict) -> str result.
    """
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    handler: Callable = None


class BasePlugin:
    """Base class for all G Assistant plugins.

    Subclass this and implement get_intents() and/or get_tools().
    Place your plugin in plugins/ directory — it's auto-discovered on startup.
    """

    # Override these in your plugin
    name: str = "unnamed_plugin"
    description: str = "No description"
    version: str = "1.0"
    author: str = "unknown"

    def __init__(self):
        self.logger = logging.getLogger(f"plugin.{self.name}")
        self._brain = None          # Set by loader after init
        self._memory = None         # Set by loader after init
        self._speak_fn = None       # Set by loader after init
        self._enabled = True

    # --- Lifecycle hooks (override as needed) ---

    def on_load(self):
        """Called after plugin is loaded and wired. Do initialization here."""
        pass

    def on_unload(self):
        """Called before plugin is unloaded. Clean up resources."""
        pass

    def on_wake(self):
        """Called when assistant transitions from IDLE to ACTIVE."""
        pass

    def on_sleep(self):
        """Called when assistant transitions from ACTIVE to IDLE."""
        pass

    # --- Intent and tool registration (override these) ---

    def get_intents(self):
        """Return list of PluginIntent objects this plugin handles.

        Intents are checked BEFORE the LLM, so they're fast (0ms regex match).
        Use for commands specific to your plugin that don't need LLM reasoning.

        Returns:
            list[PluginIntent]
        """
        return []

    def get_tools(self):
        """Return list of PluginTool objects the LLM can call.

        Tools are added to the LLM's tool list, so it can decide to call them.
        Use for capabilities that benefit from LLM reasoning about when to use them.

        Returns:
            list[PluginTool]
        """
        return []

    # --- Helper methods (available to all plugins) ---

    def quick_chat(self, prompt):
        """Send a quick LLM prompt and get a response. No tools, no history."""
        if self._brain and hasattr(self._brain, 'quick_chat'):
            return self._brain.quick_chat(prompt)
        return None

    def remember(self, key, value):
        """Store a value in persistent memory (survives restarts)."""
        if self._memory:
            self._memory.remember(f"plugin_{self.name}", key, value)

    def recall(self, key):
        """Retrieve a value from persistent memory."""
        if self._memory:
            return self._memory.recall(f"plugin_{self.name}", key)
        return None

    def speak(self, text):
        """Speak text to the user via TTS."""
        if self._speak_fn:
            self._speak_fn(text)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)
        self.logger.info(f"Plugin {'enabled' if value else 'disabled'}: {self.name}")

    def __repr__(self):
        return f"<Plugin: {self.name} v{self.version}>"
