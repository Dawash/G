"""Stateful orchestrator — state-first execution replacing screenshot-first loops.

Instead of: screenshot → LLM vision → decide → click → screenshot
Does:       observe_state → select_domain → execute_action → verify_postcondition

Falls back to vision-based approach only when no structured observer/executor
can handle the task.
"""

import logging
import time
from dataclasses import dataclass, field

from automation.executors.base import ActionSpec, ActionResult
from automation.observers.windows_observer import WindowsObserver
from automation.observers.filesystem_observer import FilesystemObserver
from automation.observers.browser_observer import BrowserObserver
from automation.observers.system_observer import SystemObserver
from automation.executors.windows_executor import WindowsExecutor
from automation.executors.filesystem_executor import FilesystemExecutor
from automation.executors.browser_executor import BrowserExecutor
from automation.verifiers.postconditions import verify

logger = logging.getLogger(__name__)


# ===================================================================
# Action catalog — typed actions with preconditions and verification
# ===================================================================

# Maps (domain, action_name) -> ActionSpec template
_ACTION_CATALOG = {
    # --- Browser ---
    ("browser", "navigate"): ActionSpec(
        name="navigate", domain="browser",
        preconditions=["browser_running"],
        verification=["url_contains:{url}"],
        fallback_chain=["cdp", "keyboard", "webbrowser"],
    ),
    ("browser", "click"): ActionSpec(
        name="click", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["cdp", "uia"],
    ),
    ("browser", "fill"): ActionSpec(
        name="fill", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["cdp", "uia", "keyboard"],
    ),
    ("browser", "read_page"): ActionSpec(
        name="read_page", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["cdp", "web_agent"],
    ),
    ("browser", "switch_tab"): ActionSpec(
        name="switch_tab", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["cdp", "keyboard"],
    ),
    ("browser", "new_tab"): ActionSpec(
        name="new_tab", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["keyboard"],
    ),
    ("browser", "close_tab"): ActionSpec(
        name="close_tab", domain="browser",
        preconditions=["browser_running"],
        fallback_chain=["keyboard"],
    ),

    # --- Windows ---
    ("windows", "focus"): ActionSpec(
        name="focus", domain="windows",
        preconditions=["window_exists:{name}"],
        verification=["window_is_focused:{name}"],
        fallback_chain=["pygetwindow", "uia"],
    ),
    ("windows", "close"): ActionSpec(
        name="close", domain="windows",
        fallback_chain=["pygetwindow", "taskkill"],
    ),
    ("windows", "snap"): ActionSpec(
        name="snap", domain="windows",
        preconditions=["window_exists:{name}"],
        fallback_chain=["pygetwindow"],
    ),
    ("windows", "open"): ActionSpec(
        name="open", domain="windows",
        fallback_chain=["action_registry", "app_finder"],
    ),
    ("windows", "minimize"): ActionSpec(
        name="minimize", domain="windows",
        fallback_chain=["pygetwindow"],
    ),

    # --- Filesystem ---
    ("filesystem", "move"): ActionSpec(
        name="move", domain="filesystem",
        preconditions=["file_exists:{src}"],
        verification=["file_exists:{dst}"],
        fallback_chain=["shutil"],
    ),
    ("filesystem", "copy"): ActionSpec(
        name="copy", domain="filesystem",
        preconditions=["file_exists:{src}"],
        verification=["file_exists:{dst}"],
        fallback_chain=["shutil"],
    ),
    ("filesystem", "delete"): ActionSpec(
        name="delete", domain="filesystem",
        preconditions=["file_exists:{path}"],
        fallback_chain=["os"],
        safe=False,
    ),
    ("filesystem", "rename"): ActionSpec(
        name="rename", domain="filesystem",
        preconditions=["file_exists:{path}"],
        fallback_chain=["os"],
    ),
    ("filesystem", "list"): ActionSpec(
        name="list", domain="filesystem",
        fallback_chain=["os"],
    ),
}


# ===================================================================
# Domain classification
# ===================================================================

_DOMAIN_PATTERNS = {
    "browser": [
        "navigate", "go to", "open url", "click", "fill", "type in",
        "read page", "page content", "switch tab", "new tab", "close tab",
        "browser", "webpage", "website", "search on page",
    ],
    "windows": [
        "focus", "switch to", "open app", "close app", "minimize", "maximize",
        "snap", "arrange", "window", "process", "launch", "kill",
    ],
    "filesystem": [
        "move file", "copy file", "rename", "delete file", "zip", "unzip",
        "list files", "find file", "folder", "directory",
    ],
    "system": [
        "cpu", "ram", "memory", "disk space", "storage", "battery",
        "network", "ip address", "uptime", "hostname", "system info",
        "processes", "top processes",
    ],
}


def classify_domain(action_name, args=None):
    """Classify which domain an action belongs to.

    Args:
        action_name: Tool name or action description.
        args: Optional dict of arguments for context.

    Returns:
        str: "browser", "windows", "filesystem", or "unknown"
    """
    lower = action_name.lower()

    # Direct tool name mapping
    _TOOL_DOMAIN = {
        "browser_action": "browser",
        "browser_navigate": "browser",
        "focus_window": "windows",
        "open_app": "windows",
        "close_app": "windows",
        "minimize_app": "windows",
        "snap_window": "windows",
        "list_windows": "windows",
        "manage_files": "filesystem",
        "run_terminal": "system",
    }

    if lower in _TOOL_DOMAIN:
        return _TOOL_DOMAIN[lower]

    # create_file needs the full handler (LLM content generation) — don't intercept
    if lower == "create_file":
        return "unknown"

    # Check if args hint at browser (has URL)
    if args:
        if args.get("url") or args.get("selector"):
            return "browser"
        if args.get("path") or args.get("destination"):
            return "filesystem"

    # Keyword match
    for domain, keywords in _DOMAIN_PATTERNS.items():
        if any(kw in lower for kw in keywords):
            return domain

    return "unknown"


# ===================================================================
# Orchestrator
# ===================================================================

class StatefulOrchestrator:
    """State-first execution engine.

    Replaces the screenshot→LLM→click loop with:
    1. Read structured state via observers
    2. Execute via domain-specific executors
    3. Verify postconditions
    4. Fall back to vision only when necessary
    """

    def __init__(self, action_registry=None):
        self.windows_obs = WindowsObserver()
        self.filesystem_obs = FilesystemObserver()
        self.browser_obs = BrowserObserver()
        self.system_obs = SystemObserver()

        self.windows_exec = WindowsExecutor(self.windows_obs)
        self.filesystem_exec = FilesystemExecutor(self.filesystem_obs)
        self.browser_exec = BrowserExecutor(self.browser_obs)

        self._action_registry = action_registry or {}
        self._last_result = None

    def can_handle(self, tool_name, args=None):
        """Check if this action can be handled state-first (no vision needed).

        Returns:
            bool
        """
        domain = classify_domain(tool_name, args)
        return domain != "unknown"

    def execute(self, tool_name, args=None):
        """Execute a tool action via state-first approach.

        Args:
            tool_name: Tool name (e.g. "browser_action", "focus_window").
            args: Dict of tool arguments.

        Returns:
            ActionResult, or None if this orchestrator can't handle it.
        """
        args = args or {}
        domain = classify_domain(tool_name, args)

        if domain == "unknown":
            return None

        t0 = time.perf_counter()

        try:
            if domain == "browser":
                result = self._execute_browser(tool_name, args)
            elif domain == "windows":
                result = self._execute_windows(tool_name, args)
            elif domain == "filesystem":
                result = self._execute_filesystem(tool_name, args)
            elif domain == "system":
                result = self._execute_system(tool_name, args)
            else:
                return None

            if result:
                result.duration_ms = int((time.perf_counter() - t0) * 1000)
                self._last_result = result
                logger.debug(f"Orchestrator: {tool_name} → {result.strategy_used} "
                           f"({'OK' if result.ok else 'FAIL'}) {result.duration_ms}ms")
            return result

        except Exception as e:
            logger.error(f"Orchestrator error ({tool_name}): {e}")
            return ActionResult(ok=False, error=str(e))

    def execute_spec(self, spec, args=None):
        """Execute an ActionSpec directly.

        For the planner to use when it builds explicit action sequences.
        """
        args = args or spec.args

        # Check preconditions
        if spec.preconditions:
            resolved = [p.format(**args) for p in spec.preconditions]
            ok, details = verify(resolved)
            if not ok:
                failed = [d for d in details if not d["passed"]]
                return ActionResult(
                    ok=False,
                    error=f"Precondition failed: {failed[0]['condition']}",
                )

        # Execute
        result = self.execute(spec.name, args)

        # Verify postconditions
        if result and result.ok and spec.verification:
            resolved = [v.format(**args) for v in spec.verification]
            ok, details = verify(resolved)
            result.verified = ok

        return result

    def get_world_state(self):
        """Get a combined snapshot from all observers.

        Returns:
            dict with windows, browser, filesystem state.
        """
        state = {}

        try:
            win_obs = self.windows_obs.observe()
            state["windows"] = win_obs.data
        except Exception:
            state["windows"] = {}

        try:
            browser_obs = self.browser_obs.observe()
            state["browser"] = browser_obs.data
        except Exception:
            state["browser"] = {}

        try:
            system_obs = self.system_obs.observe()
            state["system"] = system_obs.data
        except Exception:
            state["system"] = {}

        return state

    # ---------------------------------------------------------------
    # Domain dispatchers
    # ---------------------------------------------------------------

    def _execute_browser(self, tool_name, args):
        """Route browser actions to BrowserExecutor."""
        action = args.get("action", "")
        lower = tool_name.lower()

        # Determine sub-action
        if "navigate" in lower or action == "navigate":
            url = args.get("url", "")
            return self.browser_exec.navigate(url)

        elif action == "click" or "click" in lower:
            return self.browser_exec.click_element(
                selector=args.get("selector"),
                text=args.get("text"),
            )

        elif action == "fill" or "fill" in lower:
            return self.browser_exec.fill_field(
                selector=args.get("selector"),
                field_name=args.get("field_name"),
                text=args.get("text", ""),
            )

        elif action == "read" or "read" in lower:
            return self.browser_exec.read_page(
                selector=args.get("selector"),
            )

        elif action == "switch_tab":
            return self.browser_exec.switch_tab(
                index=args.get("index"),
                title=args.get("title"),
            )

        elif action == "new_tab":
            return self.browser_exec.new_tab(args.get("url"))

        elif action == "close_tab":
            return self.browser_exec.close_tab()

        elif action == "back":
            return self.browser_exec.go_back()

        elif action == "snapshot":
            return self.browser_exec.get_page_snapshot()

        # Generic browser_action with sub-action
        return None

    def _execute_windows(self, tool_name, args):
        """Route window actions to WindowsExecutor."""
        lower = tool_name.lower()
        name = args.get("name", "")

        if "focus" in lower or "switch" in lower:
            return self.windows_exec.focus_window(name)

        elif "close" in lower:
            return self.windows_exec.close_window(name)

        elif "open" in lower or "launch" in lower:
            return self.windows_exec.open_app(name, self._action_registry)

        elif "minimize" in lower:
            if name:
                return self.windows_exec.minimize_window(name)
            return self.windows_exec.minimize_all()

        elif "snap" in lower:
            position = args.get("position", "left")
            return self.windows_exec.snap_window(name, position)

        elif "list" in lower:
            windows = self.windows_obs.get_all_windows()
            names = [w.title[:50] for w in windows[:10]]
            return ActionResult(
                ok=True, strategy_used="win32",
                state_after={"windows": names, "count": len(windows)},
                verified=True,
                message=f"Open windows: {', '.join(names)}" if names
                        else "No windows open.",
            )

        return None

    def _execute_filesystem(self, tool_name, args):
        """Route file actions to FilesystemExecutor."""
        action = args.get("action", "").lower()

        if action == "move":
            return self.filesystem_exec.move_file(
                args.get("path", ""), args.get("destination", ""))

        elif action == "copy":
            return self.filesystem_exec.copy_file(
                args.get("path", ""), args.get("destination", ""))

        elif action == "rename":
            return self.filesystem_exec.rename_file(
                args.get("path", ""), args.get("destination", ""))

        elif action == "delete":
            return self.filesystem_exec.delete_file(args.get("path", ""))

        elif action == "zip":
            paths = args.get("path", "").split(",")
            output = args.get("destination", "archive.zip")
            return self.filesystem_exec.zip_files(
                [p.strip() for p in paths], output)

        elif action == "unzip":
            return self.filesystem_exec.unzip_file(
                args.get("path", ""), args.get("destination"))

        elif action in ("list", "find"):
            return self.filesystem_exec.list_directory(
                args.get("path"), args.get("pattern"))

        return None

    def _execute_system(self, tool_name, args):
        """Route system info queries to SystemObserver (avoids PowerShell).

        Falls through to None if query isn't a simple system info question,
        letting the caller fall back to run_terminal/PowerShell.
        """
        command = args.get("command", "")

        # Try answering via structured system observer first
        answer = self.system_obs.answer_query(command)
        if answer:
            return ActionResult(
                ok=True, strategy_used="psutil",
                state_after={"answer": answer},
                verified=True,
                message=answer,
            )

        # Top processes
        if any(k in command.lower() for k in ("top process", "most memory",
                                                "most cpu", "what's using")):
            procs = self.system_obs.get_top_processes(n=10)
            if procs:
                lines = [f"  {p['name']} (PID {p['pid']}): "
                         f"{p['memory_mb']} MB, {p['cpu_percent']}% CPU"
                         for p in procs[:10]]
                msg = "Top processes by memory:\n" + "\n".join(lines)
                return ActionResult(
                    ok=True, strategy_used="psutil",
                    state_after={"processes": procs[:10]},
                    verified=True, message=msg,
                )

        # Full system summary
        if any(k in command.lower() for k in ("system info", "system status",
                                                "system overview")):
            info = self.system_obs.get_system_info()
            return ActionResult(
                ok=True, strategy_used="psutil",
                state_after={"summary": info.summary()},
                verified=True, message=info.summary(),
            )

        # Can't handle this command via psutil — fall through to terminal
        return None

    # ---------------------------------------------------------------
    # State queries (for planner context)
    # ---------------------------------------------------------------

    def is_app_running(self, name):
        return self.windows_obs.is_window_open(name)

    def is_browser_ready(self):
        return self.browser_obs.is_browser_running()

    def get_active_window(self):
        win = self.windows_obs.get_active_window()
        return win.title if win else ""

    def get_current_url(self):
        return self.browser_obs.get_current_url()

    def get_system_summary(self):
        """Quick system state for planner context."""
        return self.system_obs.get_system_info().summary()
