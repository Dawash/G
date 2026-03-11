# tools — Registry-based tool system with execution, verification, undo, caching, safety.
#
# Phase 7 modules:
#   schemas.py       — ToolSpec metadata type
#   registry.py      — ToolRegistry (tool lookup + OpenAI schema generation)
#   executor.py      — ToolExecutor (dispatch with lifecycle hooks)
#   builtin_tools.py — First 5 migrated tools (open_app, google_search, get_weather, set_reminder, send_email)
#   verifier.py      — Post-execution verification (process/window checks)
#   undo_manager.py  — Thread-safe undo stack with time-windowed rollback
#   cache.py         — TTL-based response cache
#
# Phase 11 modules:
#   safety_policy.py — Safety levels, confirmation policies, dry-run, terminal blocklist
#   audit_log.py     — Structured JSON-lines audit log for all tool executions
#
# Phase 12 modules:
#   info_tools.py    — 9 info tools (get_forecast, get_time, get_news, list_reminders, web_read, web_search_answer, get_calendar, read_clipboard, analyze_clipboard_image)
#   action_tools.py  — 7 action tools (close_app, minimize_app, toggle_setting, system_command, run_self_test, restart_assistant, manage_alarm)
#   system_tools.py  — 3 system tools (run_terminal, manage_files, manage_software)
#   desktop_tools.py — 13 tools (play_music, search_in_app, create_file, type_text, press_key, click_at,
#                       scroll, take_screenshot, find_on_screen, click_element, manage_tabs, fill_form, agent_task)
#
# Phase 13 modules:
#   memory_workflow_tools.py — 2 tools (memory_control, run_workflow)
#
# Browser automation:
#   browser_tools.py — browser_action via persistent CDP session (overwrites desktop_tools version)
