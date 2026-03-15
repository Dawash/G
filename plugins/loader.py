"""
Plugin loader — discovers, loads, and manages plugins.

Scans the plugins/ directory for Python files/packages that export
a class inheriting from BasePlugin. Handles:
  - Auto-discovery from plugins/ directory
  - Safe loading with error isolation (bad plugin doesn't crash system)
  - Intent routing (check plugins before LLM)
  - Tool registration (add plugin tools to LLM tool list)
  - Lifecycle management (load/unload/enable/disable)
"""

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path

from plugins.base import BasePlugin, PluginIntent, PluginTool

logger = logging.getLogger(__name__)


class PluginLoader:
    """Discovers, loads, and routes to plugins."""

    def __init__(self, plugin_dir=None):
        if plugin_dir is None:
            plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
        self._plugin_dir = plugin_dir
        self._plugins = {}          # name -> BasePlugin instance
        self._intents = []          # sorted list of (priority, PluginIntent, plugin_name)
        self._tools = {}            # tool_name -> (PluginTool, plugin_name)
        self._brain = None
        self._memory = None
        self._speak_fn = None

    def set_brain(self, brain):
        """Wire the brain reference for plugin quick_chat access."""
        self._brain = brain
        for p in self._plugins.values():
            p._brain = brain

    def set_memory(self, memory_store):
        """Wire the memory store for plugin persistent storage."""
        self._memory = memory_store
        for p in self._plugins.values():
            p._memory = memory_store

    def set_speak_fn(self, speak_fn):
        """Wire the speak function for plugin TTS access."""
        self._speak_fn = speak_fn
        for p in self._plugins.values():
            p._speak_fn = speak_fn

    def discover_and_load(self):
        """Scan plugins/ directory and load all valid plugins.

        Looks for:
          - plugins/*.py files (single-file plugins)
          - plugins/*/__ init__.py packages (multi-file plugins)
        Skips: base.py, loader.py, __init__.py, __pycache__
        """
        skip = {"base.py", "loader.py", "__init__.py", "conftest.py"}
        loaded = 0
        errors = 0

        plugin_path = Path(self._plugin_dir)
        if not plugin_path.exists():
            logger.warning(f"Plugin directory not found: {plugin_path}")
            return loaded, errors

        # Single-file plugins: plugins/my_plugin.py
        for py_file in sorted(plugin_path.glob("*.py")):
            if py_file.name in skip or py_file.name.startswith("_"):
                continue
            ok = self._load_file(py_file)
            if ok:
                loaded += 1
            else:
                errors += 1

        # Package plugins: plugins/my_plugin/__init__.py
        for subdir in sorted(plugin_path.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith("_"):
                continue
            init_file = subdir / "__init__.py"
            if init_file.exists():
                ok = self._load_file(init_file, package_name=subdir.name)
                if ok:
                    loaded += 1
                else:
                    errors += 1

        # Sort intents by priority (highest first)
        self._intents.sort(key=lambda x: -x[0])

        logger.info(f"Plugins loaded: {loaded} OK, {errors} failed, "
                     f"{len(self._intents)} intents, {len(self._tools)} tools")
        return loaded, errors

    def _load_file(self, file_path, package_name=None):
        """Load a single plugin file. Returns True on success."""
        module_name = package_name or file_path.stem
        full_module = f"plugins.{module_name}"

        try:
            # Import the module
            spec = importlib.util.spec_from_file_location(full_module, str(file_path))
            if not spec or not spec.loader:
                logger.warning(f"Plugin skip (no spec): {file_path.name}")
                return False

            module = importlib.util.module_from_spec(spec)
            sys.modules[full_module] = module
            spec.loader.exec_module(module)

            # Find the plugin class (first BasePlugin subclass in the module)
            plugin_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type)
                        and issubclass(attr, BasePlugin)
                        and attr is not BasePlugin):
                    plugin_class = attr
                    break

            if not plugin_class:
                logger.debug(f"Plugin skip (no BasePlugin subclass): {file_path.name}")
                return False

            # Instantiate and wire
            plugin = plugin_class()
            plugin._brain = self._brain
            plugin._memory = self._memory
            plugin._speak_fn = self._speak_fn

            # Register intents
            for intent in plugin.get_intents():
                self._intents.append((intent.priority, intent, plugin.name))

            # Register tools
            for tool in plugin.get_tools():
                if tool.name in self._tools:
                    logger.warning(f"Plugin tool name collision: {tool.name} "
                                   f"({plugin.name} vs {self._tools[tool.name][1]})")
                else:
                    self._tools[tool.name] = (tool, plugin.name)

            # Call on_load lifecycle hook
            plugin.on_load()

            self._plugins[plugin.name] = plugin
            logger.info(f"Plugin loaded: {plugin.name} v{plugin.version} "
                         f"({len(plugin.get_intents())} intents, {len(plugin.get_tools())} tools)")
            return True

        except Exception as e:
            logger.error(f"Plugin load failed ({file_path.name}): {e}", exc_info=True)
            # Clean up partial module registration
            sys.modules.pop(full_module, None)
            return False

    def try_handle(self, user_input):
        """Try to match user input against plugin intents.

        Called from brain.think() BEFORE the LLM, so plugins get first priority
        for commands they've registered patterns for.

        Args:
            user_input: The user's text input.

        Returns:
            str response if a plugin handled it, None if no match.
        """
        for priority, intent, plugin_name in self._intents:
            plugin = self._plugins.get(plugin_name)
            if not plugin or not plugin.enabled:
                continue
            match = intent.match(user_input)
            if match:
                try:
                    result = intent.handler(user_input, match)
                    if result is not None:
                        logger.info(f"Plugin handled: {plugin_name} "
                                     f"({intent.description or intent.pattern[:30]})")
                        return str(result)
                except Exception as e:
                    logger.error(f"Plugin handler error ({plugin_name}): {e}")
        return None

    def execute_tool(self, tool_name, arguments):
        """Execute a plugin-provided tool.

        Called from brain.execute_tool() when the tool name matches a plugin tool.

        Returns:
            str result, or None if tool not found.
        """
        entry = self._tools.get(tool_name)
        if not entry:
            return None
        tool, plugin_name = entry
        plugin = self._plugins.get(plugin_name)
        if not plugin or not plugin.enabled:
            return None
        try:
            result = tool.handler(arguments)
            logger.info(f"Plugin tool executed: {tool_name} ({plugin_name})")
            return str(result) if result is not None else "Done."
        except Exception as e:
            logger.error(f"Plugin tool error ({tool_name}, {plugin_name}): {e}")
            return f"Plugin error: {e}"

    def get_tool_definitions(self):
        """Get LLM-compatible tool definitions from all plugins.

        Returns list of dicts in OpenAI function-calling format.
        These are merged into the brain's tool list.
        """
        definitions = []
        for tool_name, (tool, plugin_name) in self._tools.items():
            plugin = self._plugins.get(plugin_name)
            if not plugin or not plugin.enabled:
                continue
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            })
        return definitions

    def on_wake(self):
        """Notify all plugins that assistant woke up."""
        for plugin in self._plugins.values():
            if plugin.enabled:
                try:
                    plugin.on_wake()
                except Exception as e:
                    logger.debug(f"Plugin on_wake error ({plugin.name}): {e}")

    def on_sleep(self):
        """Notify all plugins that assistant is going to sleep."""
        for plugin in self._plugins.values():
            if plugin.enabled:
                try:
                    plugin.on_sleep()
                except Exception as e:
                    logger.debug(f"Plugin on_sleep error ({plugin.name}): {e}")

    def unload_all(self):
        """Unload all plugins (called on shutdown)."""
        for name, plugin in list(self._plugins.items()):
            try:
                plugin.on_unload()
            except Exception as e:
                logger.debug(f"Plugin unload error ({name}): {e}")
        self._plugins.clear()
        self._intents.clear()
        self._tools.clear()

    def list_plugins(self):
        """Return list of loaded plugin info dicts."""
        return [
            {
                "name": p.name,
                "description": p.description,
                "version": p.version,
                "author": p.author,
                "enabled": p.enabled,
                "intents": len(p.get_intents()),
                "tools": len(p.get_tools()),
            }
            for p in self._plugins.values()
        ]

    @property
    def plugin_count(self):
        return len(self._plugins)

    @property
    def tool_names(self):
        return list(self._tools.keys())
