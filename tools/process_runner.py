"""
Tool worker process — runs tools in isolation.

Launched as a subprocess by IsolatedToolExecutor.
Communicates via stdin/stdout JSON lines.

Protocol:
  Request:  {"id": "abc", "tool": "run_terminal", "args": {"command": "dir"}}
  Response: {"id": "abc", "ok": true, "result": "..."}
  Error:    {"id": "abc", "ok": false, "error": "...", "traceback": "..."}
"""

import json
import sys
import os
import traceback
import logging

# Ensure project root is in path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def _execute_tool(tool_name, arguments):
    """Execute a tool by name. Imports handlers dynamically."""

    # Import tool handlers based on name
    # System tools
    if tool_name == "run_terminal":
        from brain_defs import _run_terminal
        admin = arguments.get("admin", False)
        cmd = arguments.get("command", "")
        return _run_terminal(cmd, admin)

    elif tool_name == "manage_files":
        from brain_defs import _manage_files
        return _manage_files(
            arguments.get("action", ""),
            arguments.get("path", ""),
            arguments.get("destination"),
        )

    elif tool_name == "manage_software":
        from brain_defs import _manage_software
        return _manage_software(
            arguments.get("action", ""),
            arguments.get("name", ""),
        )

    # Desktop automation tools
    elif tool_name in ("click_at", "type_text", "press_key", "scroll"):
        try:
            from computer import click, type_text, press_key, scroll
            if tool_name == "click_at":
                x = arguments.get("x", 0)
                y = arguments.get("y", 0)
                return click(x, y)
            elif tool_name == "type_text":
                text = arguments.get("text", "")
                return type_text(text)
            elif tool_name == "press_key":
                key = arguments.get("keys", "") or arguments.get("key", "")
                return press_key(key)
            elif tool_name == "scroll":
                direction = arguments.get("direction", "down")
                amount = arguments.get("amount", 3)
                return scroll(direction, amount)
        except ImportError:
            return f"Desktop automation not available: {tool_name}"

    # Execution strategies CLI
    elif tool_name == "_cli_execute":
        from execution_strategies import execute_cli
        cmd = arguments.get("command", "")
        timeout = arguments.get("timeout", 30)
        return execute_cli(cmd, timeout=timeout)

    # Fallback: try tool registry
    else:
        try:
            from tools.registry import get_default
            registry = get_default()
            spec = registry.get(tool_name)
            if spec and spec.handler:
                return spec.handler(arguments=arguments)
        except Exception:
            pass
        return f"Unknown tool: {tool_name}"


def worker_main():
    """Main loop: read JSON from stdin, execute, write JSON to stdout."""
    # Signal ready
    sys.stdout.write(json.dumps({"status": "ready"}) + "\n")
    sys.stdout.flush()

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break  # stdin closed

            line = line.strip()
            if not line:
                continue

            request = json.loads(line)
            req_id = request.get("id", "")
            tool_name = request.get("tool", "")
            arguments = request.get("args", {})

            if not tool_name:
                response = {"id": req_id, "ok": False, "error": "No tool name"}
            else:
                try:
                    result = _execute_tool(tool_name, arguments)
                    response = {"id": req_id, "ok": True, "result": str(result)[:5000]}
                except Exception as e:
                    response = {
                        "id": req_id, "ok": False,
                        "error": str(e),
                        "traceback": traceback.format_exc()[:2000],
                    }

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"id": "", "ok": False, "error": "Invalid JSON"}) + "\n")
            sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stdout.write(json.dumps({"id": "", "ok": False, "error": str(e)}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    worker_main()
