"""G Assistant Plugin System — Mycroft-inspired skill architecture."""

from plugins.base import BasePlugin, PluginIntent, PluginTool
from plugins.loader import PluginLoader

__all__ = ["BasePlugin", "PluginIntent", "PluginTool", "PluginLoader"]
