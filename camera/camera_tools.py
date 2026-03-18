"""
Camera tool functions for Brain integration.

Provides tool handler functions and CAMERA_TOOLS metadata list
for registration with the ToolRegistry.

All functions accept simple arguments and return user-friendly strings.
Camera selection supports natural language ("front camera", "kitchen", "CCTV").

Usage:
    from camera.camera_tools import CAMERA_TOOLS, register_camera_tools
    register_camera_tools(registry)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Tool handler functions
# ===================================================================

def _handle_list_cameras(arguments: dict, **kwargs) -> str:
    """List all discovered and configured cameras."""
    from camera.camera_manager import camera_mgr
    cameras = camera_mgr.discover_cameras()
    return camera_mgr.list_cameras()


def _handle_open_camera(arguments: dict, **kwargs) -> str:
    """Open a camera by name or query."""
    from camera.camera_manager import camera_mgr
    query = arguments.get("camera_query", "") or arguments.get("camera", "")
    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        # Try discovery first
        camera_mgr.discover_cameras()
        cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return f"No camera found matching '{query}'. Try list_cameras first."
    return camera_mgr.open_camera(cam.id)


def _handle_close_camera(arguments: dict, **kwargs) -> str:
    """Close a camera by name or query."""
    from camera.camera_manager import camera_mgr
    query = arguments.get("camera_query", "") or arguments.get("camera", "")
    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return f"No camera found matching '{query}'."
    return camera_mgr.close_camera(cam.id)


def _handle_take_photo(arguments: dict, **kwargs) -> str:
    """Capture a photo from a camera and save to disk."""
    from camera.camera_manager import camera_mgr

    query = arguments.get("camera_query", "") or arguments.get("camera", "")
    filename = arguments.get("filename", "")

    # Find camera
    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        camera_mgr.discover_cameras()
        cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return "No camera available. Is a webcam connected?"

    # Generate filename if not provided
    if not filename:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(os.path.expanduser("~"), "Pictures", f"photo_{timestamp}.jpg")

    result = camera_mgr.save_frame(cam.id, filename)
    if result.startswith("Error"):
        return result
    return f"Photo saved to {result}"


def _handle_analyze_camera(arguments: dict, **kwargs) -> str:
    """Capture a frame and analyze it with a vision LLM."""
    from camera.camera_manager import camera_mgr
    from camera.vision_analyzer import vision

    query = arguments.get("camera_query", "") or arguments.get("camera", "")
    question = arguments.get("question", "Describe what you see.")

    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        camera_mgr.discover_cameras()
        cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return "No camera available."

    frame = camera_mgr.capture_frame(cam.id)
    if frame is None:
        return f"Could not capture frame from {cam.name}."

    return vision.analyze_frame(frame, question)


def _handle_ask_camera(arguments: dict, **kwargs) -> str:
    """Ask a question about what the camera currently sees."""
    from camera.camera_manager import camera_mgr
    from camera.vision_analyzer import vision

    question = arguments.get("question", "What do you see?")
    query = arguments.get("camera_query", "") or arguments.get("camera", "")

    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        camera_mgr.discover_cameras()
        cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return "No camera available."

    frame = camera_mgr.capture_frame(cam.id)
    if frame is None:
        return f"Could not capture frame from {cam.name}."

    return vision.analyze_frame(frame, question)


def _handle_monitor_camera(arguments: dict, **kwargs) -> str:
    """Start continuous camera monitoring."""
    from camera.camera_manager import camera_mgr
    from camera.continuous_monitor import ContinuousMonitor

    query = arguments.get("camera_query", "") or arguments.get("camera", "")
    question = arguments.get("question", "Describe what is happening.")
    interval = float(arguments.get("interval", 30))

    cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        camera_mgr.discover_cameras()
        cam = camera_mgr.find_camera_by_query(query)
    if cam is None:
        return "No camera available for monitoring."

    # Use module-level monitor instance
    global _monitor
    if _monitor is None:
        _monitor = ContinuousMonitor()

    return _monitor.start(question, camera_id=cam.id, interval=interval)


def _handle_stop_monitor(arguments: dict, **kwargs) -> str:
    """Stop continuous camera monitoring and return summary."""
    global _monitor
    if _monitor is None or not _monitor.is_running:
        return "No camera monitor is currently running."
    return _monitor.stop()


# Module-level monitor instance
_monitor: Optional["ContinuousMonitor"] = None


# ===================================================================
# Tool metadata for ToolRegistry
# ===================================================================

CAMERA_TOOLS = [
    {
        "name": "list_cameras",
        "description": (
            "List all available cameras (local webcams and IP cameras). "
            "Use for: 'what cameras do I have', 'show cameras', 'list cameras'."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _handle_list_cameras,
        "aliases": ["show_cameras", "cameras", "find_cameras", "discover_cameras"],
        "primary_arg": "",
        "core": False,
    },
    {
        "name": "open_camera",
        "description": (
            "Open/activate a camera for use. Supports webcam index or IP camera name. "
            "Use for: 'open the camera', 'turn on webcam', 'activate front camera'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera_query": {
                    "type": "string",
                    "description": "Camera name, index, or description (e.g. 'webcam', 'front', 'kitchen', '0')",
                },
            },
            "required": [],
        },
        "handler": _handle_open_camera,
        "aliases": ["activate_camera", "turn_on_camera", "start_camera"],
        "arg_aliases": {"camera": "camera_query", "name": "camera_query", "id": "camera_query"},
        "primary_arg": "camera_query",
        "core": False,
    },
    {
        "name": "close_camera",
        "description": (
            "Close/deactivate a camera. "
            "Use for: 'close the camera', 'turn off webcam', 'stop camera'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera_query": {
                    "type": "string",
                    "description": "Camera to close (name, index, or description)",
                },
            },
            "required": [],
        },
        "handler": _handle_close_camera,
        "aliases": ["deactivate_camera", "turn_off_camera", "stop_camera"],
        "arg_aliases": {"camera": "camera_query", "name": "camera_query"},
        "primary_arg": "camera_query",
        "core": False,
    },
    {
        "name": "take_photo",
        "description": (
            "Take a photo using a camera and save it. "
            "Use for: 'take a photo', 'capture image', 'take a picture', 'snap a photo'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera_query": {
                    "type": "string",
                    "description": "Which camera to use (default: first available)",
                },
                "filename": {
                    "type": "string",
                    "description": "File path to save the photo (optional, auto-generated if empty)",
                },
            },
            "required": [],
        },
        "handler": _handle_take_photo,
        "aliases": ["capture_photo", "snap_photo", "take_picture", "capture_image", "photograph"],
        "arg_aliases": {"camera": "camera_query", "path": "filename", "file": "filename"},
        "primary_arg": "camera_query",
        "core": False,
    },
    {
        "name": "analyze_camera",
        "description": (
            "Capture a frame from a camera and analyze it with AI vision. "
            "Use for: 'what does the camera see', 'analyze camera feed', "
            "'describe what the camera shows'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to analyze or look for in the camera feed",
                },
                "camera_query": {
                    "type": "string",
                    "description": "Which camera to use (default: first available)",
                },
            },
            "required": [],
        },
        "handler": _handle_analyze_camera,
        "aliases": ["camera_analyze", "see_camera", "camera_vision", "look_camera"],
        "arg_aliases": {"camera": "camera_query", "prompt": "question"},
        "primary_arg": "question",
        "core": False,
    },
    {
        "name": "ask_camera",
        "description": (
            "Ask a question about what the camera currently sees. "
            "Use for: 'is anyone at the door', 'what is in front of me', "
            "'can you see my keys', 'how many people are in the room'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question about the camera view",
                },
                "camera_query": {
                    "type": "string",
                    "description": "Which camera to use (default: first available)",
                },
            },
            "required": ["question"],
        },
        "handler": _handle_ask_camera,
        "aliases": ["camera_question", "look_at", "camera_see", "what_camera_sees"],
        "arg_aliases": {"camera": "camera_query", "q": "question", "prompt": "question"},
        "primary_arg": "question",
        "core": True,  # Core tool — available to local models
    },
    {
        "name": "monitor_camera",
        "description": (
            "Start continuous camera monitoring — periodically captures and analyzes frames. "
            "Use for: 'watch the camera', 'monitor the front door', "
            "'keep an eye on the room', 'alert me if someone enters'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to watch for (e.g. 'tell me if anyone enters')",
                },
                "camera_query": {
                    "type": "string",
                    "description": "Which camera to monitor (default: first available)",
                },
                "interval": {
                    "type": "number",
                    "description": "Seconds between captures (min 10, default 30)",
                },
            },
            "required": ["question"],
        },
        "handler": _handle_monitor_camera,
        "aliases": ["watch_camera", "start_monitoring", "camera_watch", "surveillance"],
        "arg_aliases": {"camera": "camera_query", "prompt": "question", "seconds": "interval"},
        "primary_arg": "question",
        "core": False,
    },
    {
        "name": "stop_monitor",
        "description": (
            "Stop continuous camera monitoring and get a summary. "
            "Use for: 'stop monitoring', 'stop watching', 'end surveillance'."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _handle_stop_monitor,
        "aliases": ["stop_monitoring", "end_monitor", "stop_watching", "stop_surveillance"],
        "primary_arg": "",
        "core": False,
    },
]


# ===================================================================
# Registry integration
# ===================================================================

def register_camera_tools(registry) -> None:
    """Register all camera tools with the ToolRegistry."""
    from tools.schemas import ToolSpec

    for tool in CAMERA_TOOLS:
        registry.register(ToolSpec(
            name=tool["name"],
            description=tool["description"],
            parameters=tool["parameters"],
            handler=tool["handler"],
            safety="safe",
            aliases=tool.get("aliases", []),
            arg_aliases=tool.get("arg_aliases", {}),
            primary_arg=tool.get("primary_arg", ""),
            core=tool.get("core", False),
        ))
    logger.info("Registered %d camera tools", len(CAMERA_TOOLS))
