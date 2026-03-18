"""
agent.desktop_agent — Desktop automation agent: observe -> think -> act -> verify.

A self-contained agent that automates desktop tasks by:
  1. Creating a step-by-step plan via LLM
  2. Observing the current screen state (a11y tree, active window)
  3. Asking the LLM what action to take next
  4. Executing the action (click, type, key press, etc.)
  5. Verifying the result and replanning on failure

Works on Windows using pywinauto (UI Automation backend) and pyautogui.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_STEPS = 30
MAX_RETRIES = 2
MAX_WAIT_SECONDS = 10
PLAN_TIMEOUT = 60
ACTION_TIMEOUT = 60


# ── Data types ───────────────────────────────────────────────────────────────

class ActionType(Enum):
    """All action types the agent can perform."""
    CLICK = auto()
    DOUBLE_CLICK = auto()
    RIGHT_CLICK = auto()
    TYPE = auto()
    PRESS_KEY = auto()
    SCROLL = auto()
    WAIT = auto()
    NAVIGATE = auto()
    FOCUS = auto()
    DONE = auto()
    FAIL = auto()


@dataclass
class Action:
    """A single desktop action decided by the LLM."""
    type: ActionType
    target: str = ""
    value: Any = None
    x: int = 0
    y: int = 0
    reasoning: str = ""


@dataclass
class StepResult:
    """Result of executing one step in the plan."""
    success: bool
    action: Optional[Action] = None
    observation_before: str = ""
    observation_after: str = ""
    error: str = ""
    duration_ms: int = 0


# ── Observer ─────────────────────────────────────────────────────────────────

class Observer:
    """Gathers information about the current desktop state."""

    @staticmethod
    def get_active_window() -> Dict[str, str]:
        """Return the title and process name of the foreground window."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            # Window title
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            # Process name
            process_name = ""
            try:
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                import psutil
                proc = psutil.Process(pid.value)
                process_name = proc.name()
            except Exception:
                pass

            return {"title": title, "process": process_name}
        except Exception as exc:
            logger.debug("get_active_window failed: %s", exc)
            return {"title": "", "process": ""}

    @staticmethod
    def get_accessibility_tree(max_elements: int = 50) -> Dict[str, Any]:
        """Return the UI Automation accessibility tree of the foreground window.

        Each element includes: name, control_type, bounding_rect, enabled.
        Falls back gracefully if pywinauto is unavailable.
        """
        elements: List[Dict[str, Any]] = []
        window_title = ""
        try:
            from pywinauto import Desktop  # type: ignore

            desktop = Desktop(backend="uia")
            wins = desktop.windows()
            if not wins:
                return {"window": "", "elements": [], "count": 0}

            # Find the foreground window
            fg = None
            try:
                user32 = ctypes.windll.user32
                hwnd = user32.GetForegroundWindow()
                for w in wins:
                    try:
                        if w.handle == hwnd:
                            fg = w
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if fg is None and wins:
                fg = wins[0]

            window_title = fg.window_text() if fg else ""

            # Enumerate descendants
            try:
                descendants = fg.descendants()
            except Exception:
                descendants = []

            count = 0
            for elem in descendants:
                if count >= max_elements:
                    break
                try:
                    name = elem.window_text().strip()
                    ctrl_type = elem.friendly_class_name()
                    if not name and ctrl_type in ("", "Pane", "Group"):
                        continue  # skip unnamed containers
                    rect = elem.rectangle()
                    enabled = elem.is_enabled()
                    elements.append({
                        "name": name,
                        "type": ctrl_type,
                        "rect": {
                            "left": rect.left,
                            "top": rect.top,
                            "right": rect.right,
                            "bottom": rect.bottom,
                        },
                        "enabled": enabled,
                    })
                    count += 1
                except Exception:
                    continue

        except ImportError:
            logger.debug("pywinauto not available, accessibility tree skipped")
            return {"window": "", "elements": [], "count": 0, "error": "pywinauto not installed"}
        except Exception as exc:
            logger.debug("get_accessibility_tree error: %s", exc)
            return {"window": window_title, "elements": elements, "count": len(elements),
                    "error": str(exc)}

        return {"window": window_title, "elements": elements, "count": len(elements)}

    @staticmethod
    def take_screenshot() -> Optional[bytes]:
        """Capture the entire screen as PNG bytes. Returns None on failure."""
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:
            logger.debug("take_screenshot failed: %s", exc)
            return None

    @staticmethod
    def get_observation_text(max_elements: int = 40) -> str:
        """Build a combined text observation for the LLM.

        Includes the active window, accessible UI elements, and a summary.
        """
        parts: List[str] = []

        # Active window
        win = Observer.get_active_window()
        parts.append(f"Active window: \"{win.get('title', '')}\" ({win.get('process', '')})")

        # Accessibility tree
        tree = Observer.get_accessibility_tree(max_elements=max_elements)
        if tree.get("window"):
            parts.append(f"Window: {tree['window']}")
        elems = tree.get("elements", [])
        if elems:
            parts.append(f"UI elements ({len(elems)} visible):")
            for i, el in enumerate(elems):
                name = el.get("name", "")
                etype = el.get("type", "")
                rect = el.get("rect", {})
                enabled = el.get("enabled", True)
                state = "" if enabled else " [disabled]"
                cx = (rect.get("left", 0) + rect.get("right", 0)) // 2 if rect else 0
                cy = (rect.get("top", 0) + rect.get("bottom", 0)) // 2 if rect else 0
                parts.append(f"  [{i}] {etype}: \"{name}\" at ({cx},{cy}){state}")
        else:
            parts.append("UI elements: none detected")
            if tree.get("error"):
                parts.append(f"  (error: {tree['error']})")

        # Screen resolution
        try:
            user32 = ctypes.windll.user32
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            parts.append(f"Screen: {w}x{h}")
        except Exception:
            pass

        return "\n".join(parts)


# ── Executor ─────────────────────────────────────────────────────────────────

class Executor:
    """Executes Action objects on the desktop."""

    @staticmethod
    def execute(action: Action) -> Tuple[bool, str]:
        """Execute a single action. Returns (success, message)."""
        try:
            atype = action.type

            if atype == ActionType.DONE:
                return True, "Task marked as done"

            if atype == ActionType.FAIL:
                return False, action.reasoning or "Agent decided to abort"

            if atype == ActionType.WAIT:
                secs = min(float(action.value or 1.0), MAX_WAIT_SECONDS)
                time.sleep(secs)
                return True, f"Waited {secs:.1f}s"

            if atype == ActionType.CLICK:
                return Executor._click(action)

            if atype == ActionType.DOUBLE_CLICK:
                return Executor._click(action, clicks=2)

            if atype == ActionType.RIGHT_CLICK:
                return Executor._click(action, button="right")

            if atype == ActionType.TYPE:
                return Executor._type_text(action)

            if atype == ActionType.PRESS_KEY:
                return Executor._press_key(action)

            if atype == ActionType.SCROLL:
                return Executor._scroll(action)

            if atype == ActionType.NAVIGATE:
                return Executor._navigate(action)

            if atype == ActionType.FOCUS:
                return Executor._focus_window(action)

            return False, f"Unknown action type: {atype}"

        except Exception as exc:
            logger.error("Executor.execute(%s) failed: %s", action.type, exc)
            return False, str(exc)

    @staticmethod
    def _click(action: Action, clicks: int = 1, button: str = "left") -> Tuple[bool, str]:
        """Click on a UI element. Try a11y name match first, fall back to coords."""
        # Attempt accessibility-based click (more reliable than coordinates)
        if action.target:
            try:
                from pywinauto import Desktop  # type: ignore

                desktop = Desktop(backend="uia")
                wins = desktop.windows()
                # Find foreground window
                user32 = ctypes.windll.user32
                hwnd = user32.GetForegroundWindow()
                fg = None
                for w in wins:
                    try:
                        if w.handle == hwnd:
                            fg = w
                            break
                    except Exception:
                        continue
                if fg:
                    descendants = fg.descendants()
                    target_lower = action.target.lower()
                    for elem in descendants:
                        try:
                            name = elem.window_text().strip()
                            if name and target_lower in name.lower():
                                if elem.is_enabled():
                                    if clicks == 2:
                                        elem.double_click_input()
                                    elif button == "right":
                                        elem.right_click_input()
                                    else:
                                        elem.click_input()
                                    return True, f"Clicked '{name}' via accessibility"
                        except Exception:
                            continue
            except ImportError:
                pass
            except Exception as exc:
                logger.debug("A11y click failed for '%s': %s", action.target, exc)

        # Fall back to coordinate click
        if action.x or action.y:
            import pyautogui  # type: ignore
            if clicks == 2:
                pyautogui.doubleClick(action.x, action.y)
            elif button == "right":
                pyautogui.rightClick(action.x, action.y)
            else:
                pyautogui.click(action.x, action.y)
            return True, f"Clicked at ({action.x}, {action.y})"

        return False, "No target name or coordinates for click"

    @staticmethod
    def _type_text(action: Action) -> Tuple[bool, str]:
        """Type text. Uses clipboard paste for non-ASCII, pyautogui for ASCII."""
        text = str(action.value or "")
        if not text:
            return False, "No text to type"

        import pyautogui  # type: ignore

        # Check if text is pure ASCII
        try:
            text.encode("ascii")
            is_ascii = True
        except UnicodeEncodeError:
            is_ascii = False

        if is_ascii:
            pyautogui.typewrite(text, interval=0.02)
        else:
            # Use clipboard for Unicode text
            try:
                import pyperclip  # type: ignore
                old_clip = pyperclip.paste()
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.1)
                pyperclip.copy(old_clip)
            except ImportError:
                # Fallback: use win32 clipboard directly
                try:
                    subprocess.run(
                        ["powershell", "-Command", f"Set-Clipboard -Value '{text}'"],
                        timeout=5, capture_output=True,
                    )
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.1)
                except Exception as exc:
                    return False, f"Unicode paste failed: {exc}"

        return True, f"Typed {len(text)} characters"

    @staticmethod
    def _press_key(action: Action) -> Tuple[bool, str]:
        """Press a key or key combination (e.g., 'enter', 'ctrl+s', 'alt+f4')."""
        key_str = str(action.value or "").strip()
        if not key_str:
            return False, "No key specified"

        import pyautogui  # type: ignore

        # Handle key combinations like ctrl+s, alt+f4
        if "+" in key_str:
            keys = [k.strip() for k in key_str.split("+")]
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(key_str)

        return True, f"Pressed '{key_str}'"

    @staticmethod
    def _scroll(action: Action) -> Tuple[bool, str]:
        """Scroll at the current position or specified coordinates."""
        import pyautogui  # type: ignore

        amount = int(action.value or 3)
        if action.x or action.y:
            pyautogui.scroll(amount, x=action.x, y=action.y)
            return True, f"Scrolled {amount} at ({action.x}, {action.y})"
        else:
            pyautogui.scroll(amount)
            return True, f"Scrolled {amount}"

    @staticmethod
    def _navigate(action: Action) -> Tuple[bool, str]:
        """Navigate a browser to a URL by typing it in the address bar."""
        url = str(action.value or "")
        if not url:
            return False, "No URL specified"

        import pyautogui  # type: ignore

        # Focus address bar, type URL, press Enter
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.3)
        pyautogui.typewrite(url, interval=0.01)
        time.sleep(0.1)
        pyautogui.press("enter")
        return True, f"Navigated to {url}"

    @staticmethod
    def _focus_window(action: Action) -> Tuple[bool, str]:
        """Bring a window to the foreground by title substring match."""
        target = str(action.target or action.value or "")
        if not target:
            return False, "No window title specified"

        try:
            from pywinauto import Desktop  # type: ignore

            desktop = Desktop(backend="uia")
            target_lower = target.lower()
            for w in desktop.windows():
                try:
                    title = w.window_text()
                    if target_lower in title.lower():
                        w.set_focus()
                        return True, f"Focused window: '{title}'"
                except Exception:
                    continue
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("Focus via pywinauto failed: %s", exc)

        # Fallback: try Alt+Tab style approach
        try:
            import pyautogui  # type: ignore
            # Use PowerShell to activate window
            ps_cmd = (
                f"$w = Get-Process | Where-Object {{$_.MainWindowTitle -like '*{target}*'}} "
                f"| Select-Object -First 1; "
                f"if ($w) {{ [void][System.Runtime.Interopservices.Marshal]::"
                f"GetDelegateForFunctionPointer("
                f"(Add-Type -MemberDefinition '"
                f"[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);'"
                f" -Name W -PassThru)::SetForegroundWindow.Method.MethodHandle.GetFunctionPointer(),"
                f" [Func[IntPtr,bool]]).Invoke($w.MainWindowHandle) }}"
            )
            # Simpler approach: just use pywinauto's find_windows
            # Already tried above, so skip complex PowerShell
            pass
        except Exception:
            pass

        return False, f"Could not find window matching '{target}'"


# ── Desktop Agent ────────────────────────────────────────────────────────────

class DesktopAgent:
    """Desktop automation agent with observe -> think -> act -> verify loop.

    Usage:
        agent = DesktopAgent(goal="Open Notepad and type hello")
        result = agent.run()
        # result: {"success": True, "message": "...", "steps": [...], "duration_ms": 1234}
    """

    def __init__(self, goal: str = "", max_steps: int = MAX_STEPS) -> None:
        self.goal = goal
        self.max_steps = max_steps
        self._steps: List[StepResult] = []
        self._plan: List[str] = []
        self._done = False
        self._retries = 0

    def run(self, on_step: Optional[Callable[[StepResult], None]] = None) -> Dict[str, Any]:
        """Execute the goal using the observe -> think -> act -> verify loop.

        Args:
            on_step: Optional callback invoked after each step with the StepResult.

        Returns:
            Dict with keys: success, message, steps, duration_ms
        """
        start = time.time()
        result: Dict[str, Any] = {
            "success": False,
            "message": "",
            "steps": [],
            "duration_ms": 0,
        }

        if not self.goal or not self.goal.strip():
            result["message"] = "No goal specified"
            return result

        logger.info("DesktopAgent starting: %s", self.goal[:80])

        try:
            # Phase 1: Create a plan
            self._plan = self._create_plan()
            if not self._plan:
                result["message"] = "Failed to create a plan"
                return result
            logger.info("Plan (%d steps): %s", len(self._plan),
                        "; ".join(s[:40] for s in self._plan))

            # Phase 2: Execute each step
            for idx, step_desc in enumerate(self._plan):
                if self._done:
                    break
                if idx >= self.max_steps:
                    logger.warning("Max steps (%d) reached", self.max_steps)
                    break

                step_result = self._execute_step(step_desc, idx, len(self._plan))
                self._steps.append(step_result)

                if on_step:
                    try:
                        on_step(step_result)
                    except Exception:
                        pass

                if step_result.action and step_result.action.type == ActionType.DONE:
                    self._done = True
                    result["success"] = True
                    result["message"] = step_result.action.reasoning or "Task completed"
                    break

                if step_result.action and step_result.action.type == ActionType.FAIL:
                    result["message"] = step_result.action.reasoning or "Agent decided to abort"
                    break

                if not step_result.success:
                    # Attempt replan
                    remaining = self._plan[idx + 1:]
                    new_plan = self._replan(step_desc, step_result.error, remaining)
                    if new_plan and self._retries < MAX_RETRIES:
                        self._retries += 1
                        logger.info("Replanning (attempt %d): %s", self._retries,
                                    "; ".join(s[:40] for s in new_plan))
                        # Replace remaining plan steps
                        self._plan = self._plan[:idx + 1] + new_plan
                    else:
                        result["message"] = f"Step failed: {step_result.error}"
                        break

            # If we exhausted all steps without DONE/FAIL, check final state
            if not self._done and not result["message"]:
                # Check if the last step was successful — task may have completed
                if self._steps and self._steps[-1].success:
                    result["success"] = True
                    result["message"] = "All plan steps completed"
                else:
                    result["message"] = "Plan exhausted without clear completion"

        except Exception as exc:
            logger.error("DesktopAgent.run() error: %s", exc, exc_info=True)
            result["message"] = f"Agent error: {exc}"

        # Compute timing
        elapsed = int((time.time() - start) * 1000)
        result["duration_ms"] = elapsed
        result["steps"] = [
            {
                "action": {
                    "type": sr.action.type.name if sr.action else "NONE",
                    "target": sr.action.target if sr.action else "",
                    "value": sr.action.value if sr.action else None,
                    "reasoning": sr.action.reasoning if sr.action else "",
                },
                "success": sr.success,
                "error": sr.error,
                "duration_ms": sr.duration_ms,
            }
            for sr in self._steps
        ]

        logger.info("DesktopAgent finished: success=%s, steps=%d, %dms — %s",
                     result["success"], len(self._steps), elapsed,
                     result["message"][:80])

        # Record metrics
        try:
            from core.observability import metrics
            if result["success"]:
                metrics.record_success("agent.task", duration_ms=elapsed)
            else:
                metrics.record_failure("agent.task", error=result.get("message", ""),
                                       duration_ms=elapsed)
        except Exception:
            pass

        # Learn successful sequences as skills
        if result["success"] and len(self._steps) >= 2:
            try:
                from memory.memory_api import memory
                memory.learn_skill(self.goal, result["steps"])
            except Exception:
                pass

        return result

    def step(self, action: Action) -> bool:
        """Execute a single manual action and record the result."""
        obs_before = Observer.get_observation_text(max_elements=20)
        t0 = time.time()
        success, msg = Executor.execute(action)
        elapsed = int((time.time() - t0) * 1000)
        obs_after = Observer.get_observation_text(max_elements=20)

        sr = StepResult(
            success=success,
            action=action,
            observation_before=obs_before,
            observation_after=obs_after,
            error="" if success else msg,
            duration_ms=elapsed,
        )
        self._steps.append(sr)
        return success

    # ── Planning ─────────────────────────────────────────────────────────────

    def _create_plan(self) -> List[str]:
        """Ask the LLM to decompose the goal into a list of step descriptions."""
        system = (
            "You are a Windows desktop automation planner. "
            "Given a user goal, produce a JSON array of step descriptions. "
            "Each step should be a short, actionable sentence. "
            "Include a final step to verify the task is complete. "
            "Keep the plan concise — typically 3-8 steps. "
            "Reply with ONLY the JSON array, no markdown, no explanation."
        )
        prompt = (
            f"Goal: {self.goal}\n\n"
            f"Current state:\n{Observer.get_observation_text(max_elements=20)}\n\n"
            f"Produce a JSON array of step descriptions to accomplish this goal on Windows."
        )

        try:
            from llm.model_router import model_router
            response = model_router.chat(prompt, task="plan", system_prompt=system)
        except Exception as exc:
            logger.error("Plan LLM call failed: %s", exc)
            # Fallback: single-step plan
            return [self.goal]

        if not response:
            return [self.goal]

        plan = self._parse_json_array(response)
        if plan and isinstance(plan, list) and all(isinstance(s, str) for s in plan):
            return plan

        # Fallback: split response into lines
        lines = [ln.strip().lstrip("0123456789.-) ") for ln in response.strip().splitlines()
                  if ln.strip() and not ln.strip().startswith(("{", "[", "]", "}"))]
        return lines if lines else [self.goal]

    # ── Step execution ───────────────────────────────────────────────────────

    def _execute_step(self, step_desc: str, idx: int, total: int) -> StepResult:
        """Execute one plan step: observe -> think -> act -> verify."""
        t0 = time.time()

        # Observe
        observation = Observer.get_observation_text(max_elements=40)

        # Think — ask LLM what action to take
        action = self._decide_action(step_desc, observation, idx, total)

        if action is None:
            return StepResult(
                success=False,
                observation_before=observation,
                error="LLM failed to decide an action",
                duration_ms=int((time.time() - t0) * 1000),
            )

        # Act
        success, msg = Executor.execute(action)

        # Short pause to let the UI settle
        if action.type not in (ActionType.DONE, ActionType.FAIL, ActionType.WAIT):
            time.sleep(0.3)

        # Verify — observe after
        observation_after = Observer.get_observation_text(max_elements=20)

        elapsed = int((time.time() - t0) * 1000)
        return StepResult(
            success=success,
            action=action,
            observation_before=observation,
            observation_after=observation_after,
            error="" if success else msg,
            duration_ms=elapsed,
        )

    def _decide_action(self, step: str, observation: str, idx: int,
                       total: int) -> Optional[Action]:
        """Ask the LLM to decide what desktop action to take.

        Returns an Action object, or None if the LLM response is unparseable.
        """
        # Build history of recent actions for context
        recent = ""
        if self._steps:
            recent_items = self._steps[-3:]  # last 3 steps
            lines = []
            for sr in recent_items:
                if sr.action:
                    status = "OK" if sr.success else f"FAILED: {sr.error}"
                    lines.append(
                        f"  {sr.action.type.name} target=\"{sr.action.target}\" "
                        f"value=\"{sr.action.value}\" → {status}"
                    )
            if lines:
                recent = "Recent actions:\n" + "\n".join(lines) + "\n\n"

        system = (
            "You are a Windows desktop automation agent. You observe the screen state "
            "and decide the next action to take.\n\n"
            "Available action types:\n"
            "  CLICK — click a UI element (provide target name or x,y coordinates)\n"
            "  DOUBLE_CLICK — double-click a UI element\n"
            "  RIGHT_CLICK — right-click a UI element\n"
            "  TYPE — type text (provide the text as value)\n"
            "  PRESS_KEY — press a key or combo like 'enter', 'ctrl+s', 'alt+f4'\n"
            "  SCROLL — scroll (positive=up, negative=down)\n"
            "  WAIT — wait N seconds for something to load\n"
            "  NAVIGATE — open a URL in the browser (Ctrl+L, type URL, Enter)\n"
            "  FOCUS — bring a window to the foreground by title\n"
            "  DONE — the task is complete\n"
            "  FAIL — the task cannot be completed\n\n"
            "Reply with ONLY a JSON object:\n"
            '{"type": "CLICK", "target": "element name", "value": null, '
            '"x": 500, "y": 300, "reasoning": "why"}\n\n'
            "For TYPE: set value to the text to type.\n"
            "For PRESS_KEY: set value to the key name.\n"
            "For DONE/FAIL: set reasoning to explain.\n"
            "If you use a target name, I will try accessibility click first. "
            "Provide x,y as fallback coordinates.\n"
            "Reply with ONLY the JSON object, no markdown, no explanation."
        )

        prompt = (
            f"Goal: {self.goal}\n"
            f"Current step ({idx + 1}/{total}): {step}\n\n"
            f"{recent}"
            f"Current screen state:\n{observation}\n\n"
            f"What single action should I take next?"
        )

        try:
            from llm.model_router import model_router
            response = model_router.chat(prompt, task="tool_call", system_prompt=system)
        except Exception as exc:
            logger.error("Decide action LLM call failed: %s", exc)
            return None

        if not response:
            return None

        return self._parse_action(response)

    def _parse_action(self, response: str) -> Optional[Action]:
        """Parse an LLM response into an Action object."""
        obj = self._parse_json_object(response)
        if not obj:
            return None

        # Parse action type
        type_str = str(obj.get("type", "")).upper().strip()
        action_type = None
        for at in ActionType:
            if at.name == type_str:
                action_type = at
                break
        if action_type is None:
            logger.debug("Unknown action type in LLM response: %s", type_str)
            return None

        return Action(
            type=action_type,
            target=str(obj.get("target", "") or ""),
            value=obj.get("value"),
            x=int(obj.get("x", 0) or 0),
            y=int(obj.get("y", 0) or 0),
            reasoning=str(obj.get("reasoning", "") or ""),
        )

    # ── Replanning ───────────────────────────────────────────────────────────

    def _replan(self, failed_step: str, error: str,
                remaining: List[str]) -> Optional[List[str]]:
        """Ask the LLM to create a new plan after a step failure."""
        if self._retries >= MAX_RETRIES:
            return None

        system = (
            "You are a Windows desktop automation planner. A step in the plan failed. "
            "Given the failure info and remaining steps, produce a revised JSON array "
            "of step descriptions that works around the problem. "
            "Reply with ONLY the JSON array."
        )
        prompt = (
            f"Goal: {self.goal}\n\n"
            f"Failed step: {failed_step}\n"
            f"Error: {error}\n\n"
            f"Remaining steps from original plan: {json.dumps(remaining)}\n\n"
            f"Current state:\n{Observer.get_observation_text(max_elements=20)}\n\n"
            f"Produce a revised JSON array of steps to complete the goal."
        )

        try:
            from llm.model_router import model_router
            response = model_router.chat(prompt, task="plan", system_prompt=system)
        except Exception as exc:
            logger.debug("Replan LLM call failed: %s", exc)
            return None

        if not response:
            return None

        plan = self._parse_json_array(response)
        if plan and isinstance(plan, list):
            return plan

        return None

    # ── JSON parsing helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_json_array(text: str) -> Optional[List]:
        """Extract a JSON array from text, handling markdown fences and junk."""
        if not text:
            return None

        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = text.strip()

        # Try direct parse
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Find first [ ... ] in the text
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _parse_json_object(text: str) -> Optional[Dict]:
        """Extract a JSON object from text, handling markdown fences and junk."""
        if not text:
            return None

        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = text.strip()

        # Try direct parse
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Find first { ... } (greedy, handles nested braces)
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        result = json.loads(text[start:i + 1])
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        pass
                    start = -1

        return None


# ── Backward-compatible aliases ──────────────────────────────────────────────

ObservationStrategy = Observer
ActionExecutor = Executor
