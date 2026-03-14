"""
G -- Personal AI Operating System
===================================

Compatibility shim — delegates to orchestration.assistant_loop.run().

This file remains as the public entry point for backward compatibility.
All logic has been extracted to the orchestration/ package:
  - orchestration/assistant_loop.py   — main run() loop + helpers
  - orchestration/command_router.py   — meta-commands, exit/connect/provider detection
  - orchestration/session_manager.py  — startup greeting, auto-sleep, provider switch
  - orchestration/response_dispatcher.py — say(), llm_response(), truncate_for_speech()
  - orchestration/fallback_router.py  — keyword fallback action map
"""

import logging

from speech import stop_speaking

# --- Emergency stop (public API, re-exported for backward compat) ---

from core.state import RuntimeState as _RuntimeState
_runtime_state = _RuntimeState()

from core.control_flags import (
    emergency_stop_service as _estop,
    trigger_emergency_stop,
    clear_emergency_stop,
    is_emergency_stopped,
)

# Register cleanup callbacks: cancel desktop agent + stop TTS on emergency stop
def _cancel_agent():
    try:
        from desktop_agent import DesktopAgent
        if DesktopAgent._active_instance:
            DesktopAgent._active_instance.cancel()
    except Exception:
        pass

def _stop_tts():
    try:
        stop_speaking()
    except Exception:
        pass

_estop.on_stop(_cancel_agent)
_estop.on_stop(_stop_tts)

logger = logging.getLogger(__name__)


# --- Main entry point ---

def run():
    """Main assistant loop — delegates to orchestration.assistant_loop."""
    from orchestration.assistant_loop import run as _loop_run
    _loop_run(runtime_state=_runtime_state)


