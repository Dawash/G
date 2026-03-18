"""Desktop automation agent — observe -> think -> act -> verify loop."""

from agent.desktop_agent import (
    DesktopAgent,
    Observer as ObservationStrategy,
    Executor as ActionExecutor,
    ActionType,
    Action,
    StepResult,
)

__all__ = [
    "DesktopAgent", "ObservationStrategy", "ActionExecutor",
    "ActionType", "Action", "StepResult",
]
