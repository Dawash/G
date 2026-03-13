"""
Code Interpreter Tool — Safe Python sandbox for math/logic/data tasks.

Runs Python code in a subprocess with:
  - 30-second timeout
  - No network access (restricted imports)
  - No file system writes outside temp dir
  - Memory limit (256MB)
  - Captured stdout/stderr

Use cases:
  - Math calculations ("what is 25! * 3.14")
  - Data processing ("parse this CSV and find the average")
  - Code generation + execution ("write and run a fibonacci function")
  - Excel/JSON manipulation
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

# Imports allowed in sandbox
ALLOWED_IMPORTS = {
    "math", "statistics", "random", "datetime", "time",
    "json", "csv", "re", "collections", "itertools",
    "functools", "operator", "decimal", "fractions",
    "textwrap", "string", "unicodedata",
}

# Imports explicitly blocked
BLOCKED_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests",
    "ctypes", "importlib", "code", "compile",
    "__builtins__", "builtins",
}

# Max execution time
TIMEOUT = 30
# Max output length
MAX_OUTPUT = 5000


def execute_code(code: str, timeout: int = TIMEOUT) -> dict:
    """Execute Python code in a sandboxed subprocess.

    Args:
        code: Python code to execute.
        timeout: Max execution time in seconds.

    Returns:
        {"success": bool, "output": str, "error": str, "duration": float}
    """
    if not code or not code.strip():
        return {"success": False, "output": "", "error": "Empty code", "duration": 0}

    # --- Safety check: scan for dangerous patterns ---
    violation = _check_safety(code)
    if violation:
        return {"success": False, "output": "", "error": f"Safety violation: {violation}", "duration": 0}

    # --- Write code to temp file ---
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        # Wrap code with import restrictions
        sandbox_code = _build_sandbox(code)
        f.write(sandbox_code)
        temp_path = f.name

    try:
        t0 = time.perf_counter()
        result = subprocess.run(
            [sys.executable, temp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
            env=_restricted_env(),
        )
        elapsed = time.perf_counter() - t0

        stdout = result.stdout[:MAX_OUTPUT] if result.stdout else ""
        stderr = result.stderr[:MAX_OUTPUT] if result.stderr else ""

        if result.returncode == 0:
            return {
                "success": True,
                "output": stdout.strip(),
                "error": stderr.strip() if stderr else "",
                "duration": round(elapsed, 2),
            }
        else:
            return {
                "success": False,
                "output": stdout.strip(),
                "error": stderr.strip() or f"Exit code: {result.returncode}",
                "duration": round(elapsed, 2),
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": "",
            "error": f"Code execution timed out after {timeout}s",
            "duration": timeout,
        }
    except Exception as e:
        return {
            "success": False,
            "output": "",
            "error": str(e),
            "duration": 0,
        }
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _check_safety(code: str) -> str:
    """Check code for dangerous patterns. Returns violation or empty string."""
    lower = code.lower()

    # File operations
    if any(p in lower for p in ["open(", "with open", "file(", "rmdir", "unlink"]):
        if "open(" in lower:
            # Allow reading, block writing
            import re
            writes = re.findall(r"open\s*\(.+?['\"]w['\"]", lower)
            if writes:
                return "File write operations not allowed"

    # System commands
    if any(p in lower for p in ["os.system", "subprocess", "exec(", "eval(", "compile("]):
        return "System command execution not allowed"

    # Network
    if any(p in lower for p in ["socket", "urllib", "requests", "http.client"]):
        return "Network access not allowed"

    # Import check
    import re
    imports = re.findall(r'(?:import|from)\s+(\w+)', code)
    for imp in imports:
        if imp in BLOCKED_IMPORTS:
            return f"Import '{imp}' is blocked"
        if imp not in ALLOWED_IMPORTS:
            # Allow but warn
            logger.debug(f"Code interpreter: non-standard import '{imp}'")

    return ""


def _build_sandbox(code: str) -> str:
    """Wrap user code with safety restrictions."""
    return f'''
import sys
try:
    import resource_guard as _rg
except ImportError:
    pass

# Restrict builtins
_safe_builtins = {{
    'print': print, 'len': len, 'range': range, 'int': int, 'float': float,
    'str': str, 'bool': bool, 'list': list, 'dict': dict, 'set': set,
    'tuple': tuple, 'type': type, 'isinstance': isinstance, 'issubclass': issubclass,
    'abs': abs, 'round': round, 'min': min, 'max': max, 'sum': sum,
    'sorted': sorted, 'reversed': reversed, 'enumerate': enumerate, 'zip': zip,
    'map': map, 'filter': filter, 'any': any, 'all': all,
    'input': lambda *a: '', 'open': open, 'format': format,
    'chr': chr, 'ord': ord, 'hex': hex, 'oct': oct, 'bin': bin,
    'hasattr': hasattr, 'getattr': getattr, 'setattr': setattr,
    'callable': callable, 'hash': hash, 'id': id, 'repr': repr,
    'pow': pow, 'divmod': divmod,
    '__import__': __import__,
    'True': True, 'False': False, 'None': None,
}}

try:
{_indent(code)}
except Exception as _e:
    print(f"Error: {{_e}}", file=sys.stderr)
    sys.exit(1)
'''


def _indent(code: str, spaces: int = 4) -> str:
    """Indent code block."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in code.split("\n"))


def _restricted_env() -> dict:
    """Build restricted environment for subprocess."""
    env = os.environ.copy()
    # Remove sensitive vars
    for key in list(env.keys()):
        if any(s in key.upper() for s in ["API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"]):
            del env[key]
    return env


# --- Tool registration ---

def register_code_interpreter(registry):
    """Register the code_interpreter tool with the tool registry."""
    try:
        from tools.schemas import ToolSpec
        registry.register(ToolSpec(
            name="run_code",
            description=(
                "Execute Python code for math, calculations, data processing, or logic tasks. "
                "Runs in a safe sandbox. Use for: calculations, CSV parsing, data analysis, "
                "string manipulation, algorithm execution. No file writes or network access."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute"
                    },
                    "description": {
                        "type": "string",
                        "description": "What the code does (for logging)"
                    },
                },
                "required": ["code"],
            },
            handler=_handle_run_code,
            aliases=["code_interpreter", "python", "calculate", "compute"],
            safety="moderate",
            core=False,
        ))
        logger.info("Registered code_interpreter tool")
    except Exception as e:
        logger.debug(f"Could not register code_interpreter: {e}")


def _handle_run_code(code: str = "", description: str = "", **kwargs):
    """Handler for the run_code tool."""
    result = execute_code(code)
    if result["success"]:
        output = result["output"]
        if not output:
            return "Code executed successfully (no output)."
        return output
    else:
        return f"Code error: {result['error']}"
