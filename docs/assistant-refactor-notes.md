# Assistant Refactor Notes

**Date**: 2026-03-07
**Phase**: 5 — Extract orchestration from assistant.py

## What Moved

| From (assistant.py) | To | Lines |
|---|---|---|
| `run()` main loop (169-454) | `orchestration/assistant_loop.py:run()` | ~250 |
| `_debug_trace()` | `orchestration/assistant_loop.py:_debug_trace()` | 15 |
| `_say()` wrapper | `orchestration/assistant_loop.py:_say()` | 3 |
| `_llm_response()` wrapper | `orchestration/assistant_loop.py:_llm_response()` | 3 |
| `_restart_process()` | `orchestration/assistant_loop.py:_restart_process()` | 5 |
| `_api_limited()` | `orchestration/assistant_loop.py:_api_limited()` | 5 |
| `_ollama_health_monitor()` closure | `orchestration/assistant_loop.py:_start_ollama_health_monitor()` | 20 |
| Meta-command detection | `orchestration/command_router.py` (Phase 3) | 139 |
| Startup greeting, auto-sleep | `orchestration/session_manager.py` (Phase 3) | 180 |
| `say()`, `llm_response()` | `orchestration/response_dispatcher.py` (Phase 3) | 114 |
| `build_action_map()` | `orchestration/fallback_router.py` (Phase 3) | 93 |

## What Remains in assistant.py

| Item | Reason |
|---|---|
| `_runtime_state` singleton | Emergency stop callbacks need it at import time; will move to container |
| Emergency stop callback registration | `_cancel_agent()` and `_stop_tts()` must register before `run()` is called |
| `run()` thin shim | Delegates to `orchestration.assistant_loop.run(_runtime_state)` |
| Re-exports: `trigger_emergency_stop`, `clear_emergency_stop`, `is_emergency_stopped` | `brain.py` formerly imported these from assistant (now from `core.control_flags`) |
| Re-exports: `_build_action_map`, `_llm_response`, `startup_greeting` | `interactive_test.py` imports these from assistant |

**assistant.py**: 496 lines -> 60 lines (88% reduction)

## Import Compatibility

| Consumer | Imports from assistant | Status |
|---|---|---|
| `main.py` | `run` | Works (thin shim delegates to assistant_loop) |
| `run.py` | `run` | Works |
| `interactive_test.py` | `_build_action_map`, `_llm_response`, `startup_greeting` | Works (re-exported) |
| `brain.py` | `is_emergency_stopped` | Already migrated to `core.control_flags` (Phase 2) |

## What Should Be Migrated Later

1. **`_runtime_state` singleton** — should come from `app/container.py` instead of being created at module level
2. **Emergency stop callback registration** — should move to `app/bootstrap.py` during container startup
3. **`interactive_test.py` re-exports** — update interactive_test.py to import directly from orchestration modules
4. **`main.py`** — could import `run` from `orchestration.assistant_loop` directly once assistant.py is fully retired

## Orchestration Package Structure

```
orchestration/
  __init__.py              — package marker
  assistant_loop.py        — main run() loop, all runtime helpers
  command_router.py        — meta-commands, exit/connect/provider detection
  session_manager.py       — startup greeting, auto-sleep, provider switch
  response_dispatcher.py   — say(), llm_response(), truncate_for_speech()
  fallback_router.py       — keyword fallback action_map builder
```
