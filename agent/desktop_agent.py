"""
agent.desktop_agent — Desktop automation agent (stub/adapter).

Provides the class API expected by integration tests while delegating
actual execution to the top-level desktop_agent module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ActionType(Enum):
    WAIT = auto()
    DONE = auto()
    CLICK = auto()
    TYPE = auto()
    PRESS_KEY = auto()
    SCROLL = auto()
    SCREENSHOT = auto()
    RUN_COMMAND = auto()


@dataclass
class Action:
    """A single agent action."""
    type: ActionType
    value: Any = None
    metadata: Dict = field(default_factory=dict)


class ObservationStrategy:
    """Observation helpers used by the agent loop."""

    @staticmethod
    def get_accessibility_tree() -> Dict:
        """Return the active window accessibility tree (best-effort)."""
        try:
            import pygetwindow as gw  # type: ignore
            win = gw.getActiveWindow()
            return {
                "title": win.title if win else "",
                "available": True,
            }
        except Exception as e:
            logger.debug("get_accessibility_tree: %s", e)
            return {"available": False, "error": str(e)}

    @staticmethod
    def take_screenshot() -> Optional[bytes]:
        """Capture the screen and return PNG bytes, or None on failure."""
        try:
            import io
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            logger.debug("take_screenshot: %s", e)
            return None


class ActionExecutor:
    """Executes Action objects on the desktop."""

    @staticmethod
    def execute(action: Action) -> bool:
        """Execute a single action. Returns True on success."""
        try:
            if action.type == ActionType.WAIT:
                import time
                time.sleep(float(action.value or 0.5))
                return True

            if action.type == ActionType.DONE:
                return True

            if action.type == ActionType.CLICK:
                import pyautogui  # type: ignore
                x, y = action.value if action.value else (0, 0)
                pyautogui.click(x, y)
                return True

            if action.type == ActionType.TYPE:
                import pyautogui  # type: ignore
                pyautogui.typewrite(str(action.value), interval=0.03)
                return True

            if action.type == ActionType.PRESS_KEY:
                import pyautogui  # type: ignore
                pyautogui.press(str(action.value))
                return True

            if action.type == ActionType.SCROLL:
                import pyautogui  # type: ignore
                x, y, amount = action.value if action.value else (0, 0, 3)
                pyautogui.scroll(amount, x=x, y=y)
                return True

            if action.type == ActionType.SCREENSHOT:
                ObservationStrategy.take_screenshot()
                return True

            if action.type == ActionType.RUN_COMMAND:
                import subprocess
                result = subprocess.run(
                    str(action.value), shell=True, capture_output=True,
                    text=True, timeout=30,
                    encoding="utf-8", errors="replace"
                )
                return result.returncode == 0

            logger.warning("Unknown action type: %s", action.type)
            return False

        except Exception as e:
            logger.error("ActionExecutor.execute(%s) failed: %s", action.type, e)
            return False


class DesktopAgent:
    """Desktop automation agent with observation/action loop.

    Wraps the legacy desktop_agent.py execute() entry point and provides
    the structured class API expected by integration tests.
    """

    MAX_STEPS = 30

    def __init__(self, goal: str = "", max_steps: int = MAX_STEPS) -> None:
        self.goal = goal
        self.max_steps = max_steps
        self._steps: List[Dict] = []
        self._done = False

    def run(self, on_step: Optional[Callable[[Dict], None]] = None) -> Dict:
        """Execute the goal using the legacy desktop agent.

        Returns a result dict with ``success``, ``steps``, ``message``.
        """
        try:
            from desktop_agent import DesktopAgent as _LegacyAgent
            agent = _LegacyAgent()
            result = agent.execute(self.goal)
            return {
                "success": bool(result),
                "message": str(result) if result else "No result",
                "steps": self._steps,
            }
        except Exception as e:
            logger.error("DesktopAgent.run() failed: %s", e)
            return {"success": False, "message": str(e), "steps": self._steps}

    def step(self, action: Action) -> bool:
        """Execute a single action and record it."""
        success = ActionExecutor.execute(action)
        obs = ObservationStrategy.get_accessibility_tree()
        self._steps.append({
            "action": {"type": action.type.name, "value": action.value},
            "success": success,
            "obs": obs,
        })
        return success
