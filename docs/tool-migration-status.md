# Tool Migration Status

**Phase 12**: All 33 tools migrated to registry-based architecture.

## Architecture

```
tools/
  schemas.py       — ToolSpec dataclass (name, handler, safety, verifier, rollback, cache, dep flags)
  registry.py      — ToolRegistry (register/lookup/OpenAI schema generation)
  executor.py      — ToolExecutor (dispatch with pre/post lifecycle hooks)
  builtin_tools.py — 5 tools (open_app, google_search, get_weather, set_reminder, send_email)
  info_tools.py    — 6 tools (get_forecast, get_time, get_news, list_reminders, web_read, web_search_answer)
  action_tools.py  — 6 tools (close_app, minimize_app, toggle_setting, system_command, run_self_test, restart_assistant)
  system_tools.py  — 3 tools (run_terminal, manage_files, manage_software)
  desktop_tools.py — 13 tools (play_music, search_in_app, create_file, desktop automation, agent_task)
  verifier.py      — Post-execution verification (process/window checks)
  undo_manager.py  — Thread-safe undo stack (30s window, 10-entry max)
  cache.py         — TTL-based response cache
  safety_policy.py — Safety levels, confirmation flow, dry-run, terminal blocklist
  audit_log.py     — Structured JSON-lines audit log
```

## How it works

1. `brain.py` creates a `ToolRegistry` + `ToolExecutor` at module level
2. Five `register_*_tools()` functions populate the registry with 33 ToolSpecs
3. `execute_tool()` checks `_REGISTRY_TOOLS` first:
   - **All 33 tools**: route through `ToolExecutor.execute()` (safety, confirmation, cache, handler, undo, audit, learning)
   - **Dynamic tools**: still use legacy `_execute_tool_inner()` for custom runtime-created tools
4. Post-execution: verification + agent escalation + create_file tracking handled in `execute_tool()`

## All Tools (33)

| Tool | Module | Safety | Cache | Rollback | Verifier | Confirm | Special |
|------|--------|--------|-------|----------|----------|---------|---------|
| `open_app` | builtin | safe | - | close_app | yes | - | - |
| `google_search` | builtin | safe | - | - | yes | - | - |
| `get_weather` | builtin | safe | 5min | - | - | - | - |
| `set_reminder` | builtin | safe | - | - | - | - | - |
| `send_email` | builtin | sensitive | - | - | - | always | - |
| `get_forecast` | info | safe | 5min | - | - | - | - |
| `get_time` | info | safe | 30s | - | - | - | - |
| `get_news` | info | safe | 10min | - | - | - | - |
| `list_reminders` | info | safe | - | - | - | - | - |
| `web_read` | info | safe | - | - | - | - | - |
| `web_search_answer` | info | safe | - | - | - | - | - |
| `close_app` | action | safe | - | open_app | - | - | - |
| `minimize_app` | action | safe | - | - | - | - | - |
| `toggle_setting` | action | moderate | - | reverse | - | - | - |
| `system_command` | action | critical | - | - | - | always | user_input |
| `run_self_test` | action | sensitive | - | - | - | always | - |
| `restart_assistant` | action | critical | - | - | - | always | - |
| `run_terminal` | system | sensitive | - | - | - | risky/admin | - |
| `manage_files` | system | sensitive | - | - | - | delete | - |
| `manage_software` | system | sensitive | - | - | - | install/uninstall | - |
| `play_music` | desktop | safe | - | - | yes | - | user_input, quick_chat |
| `search_in_app` | desktop | safe | - | - | yes | - | - |
| `create_file` | desktop | moderate | - | - | - | - | user_input, quick_chat |
| `type_text` | desktop | moderate | - | - | - | - | - |
| `press_key` | desktop | moderate | - | - | - | - | - |
| `click_at` | desktop | moderate | - | - | - | - | - |
| `scroll` | desktop | moderate | - | - | - | - | - |
| `take_screenshot` | desktop | safe | - | - | - | - | - |
| `find_on_screen` | desktop | safe | - | - | - | - | - |
| `click_element` | desktop | moderate | - | - | - | - | - |
| `manage_tabs` | desktop | moderate | - | - | - | - | - |
| `fill_form` | desktop | moderate | - | - | - | - | - |
| `agent_task` | desktop | critical | - | - | - | always | registry, reminder, speak |

## ToolSpec Dependency Flags

| Flag | Tools | Description |
|------|-------|-------------|
| `requires_registry` | open_app, google_search, set_reminder, close_app, minimize_app, system_command, list_reminders, get_time, agent_task | Handler needs action_registry |
| `requires_reminder_mgr` | agent_task | Handler needs reminder_mgr |
| `requires_speak_fn` | agent_task | Handler needs speak_fn |
| `requires_user_input` | system_command, play_music, create_file | Handler needs user's original text |
| `requires_quick_chat` | play_music, create_file | Handler needs LLM quick_chat function |

## Legacy Code (still exists but not reached)

- `_execute_tool_inner()` in brain.py — dead code for the 33 registered tools
- Legacy cache (`_CACHE_TTL`, `_response_cache`) — only used by legacy path
- Legacy undo (`_register_undo_for_tool`) — only used by legacy path
- Legacy confirm (`_CONFIRM_TOOLS`) — only used by legacy path
- These remain as safety net for dynamic (runtime-created) tools
