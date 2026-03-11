# Module Migration Map

Maps every old module → new target module(s) in the refactored architecture.

## Naming Rationale

Two target directories use different names than the logical ideal:

| Requested | Actual | Why |
|-----------|--------|-----|
| `speech/` | `speech_new/` | A `speech/` package would shadow root `speech.py` (production code), breaking all `import speech` calls. Will be renamed to `speech/` after `speech.py` is deleted during migration. |
| `platform/windows/` | `platform_impl/windows/` | `platform` is a Python stdlib module. A `platform/` package would shadow `import platform`. |

## Current Status

| Package | Status | Notes |
|---------|--------|-------|
| `core/` | Skeleton + partial impl | `state.py` and `events.py` have working implementations |
| `app/` | Skeleton only | Docstrings + TODO comments |
| `orchestration/` | Skeleton + partial impl | `command_router.py`, `session_manager.py`, `response_dispatcher.py`, `fallback_router.py` have working code |
| `llm/` | Skeleton + partial impl | `mode_classifier.py` and `prompt_builder.py` have working code extracted from brain.py |
| `tools/` | Skeleton + partial impl | `safety_policy.py` has working code extracted from brain.py |
| `speech_new/` | Skeleton only | Docstrings + TODO comments |
| `platform_impl/` | Skeleton + partial impl | `media.py` has working code (193 lines) extracted from brain.py |
| `automation/` | Skeleton only | Docstrings + TODO comments |
| `features/` | Skeleton only | Docstrings + TODO comments |
| `tests/` | Skeleton only | conftest.py + empty test files |

**None of the skeleton packages are wired into production yet.** The root modules remain the live code.

## Legend

- **Full** = entire file migrates to one target
- **Split** = file splits across multiple targets
- **Delete** = file removed after migration (replaced entirely)
- **Keep** = file stays as-is or becomes thin wrapper

---

## Old → New Mapping

### assistant.py (915 lines) → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Main loop, IDLE/ACTIVE state machine | `orchestration/assistant_loop.py` | Core event loop |
| Session state (last_response, inactivity timer) | `orchestration/session_manager.py` | Session lifecycle |
| Meta-commands (skip, shorter, repeat, undo, correction) | `orchestration/command_router.py` | Pre-brain command handling |
| action_map keyword fallback | `orchestration/fallback_router.py` | Offline intent → action |
| Wake word → greet → listen loop | `orchestration/assistant_loop.py` | Top-level flow |
| Emergency stop handling | `orchestration/assistant_loop.py` | Ctrl+C / voice stop |
| Daily briefing generation | `orchestration/session_manager.py` | Startup routine |

### brain.py (2,953 lines) → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Brain class (init, think, quick_chat) | `llm/brain_service.py` | Core LLM service |
| Mode classification (quick/agent/research) | `llm/mode_classifier.py` | Pre-routing |
| System prompt building | `llm/context_manager.py` | Prompt assembly |
| Context window management (messages, topic tracking) | `llm/context_manager.py` | Conversation memory |
| Response sanitization | `llm/response_builder.py` | Clean LLM output |
| execute_tool() dispatch | `tools/executor.py` | Tool execution hub |
| _execute_tool_inner() per-tool handlers | `tools/executor.py` + platform_impl/* | Dispatch + impl |
| _play_music() | `platform_impl/windows/media.py` | Spotify/media control |
| Dynamic tool factory (create_tool) | `tools/registry.py` | Runtime tool creation |
| Undo stack + _register_undo | `tools/undo_manager.py` | Reversible actions |
| Response cache | `tools/cache.py` | TTL-based caching |
| _confirm_with_user() | `tools/safety_policy.py` | Confirmation prompts |
| _validate_tool_choice() | `tools/safety_policy.py` | Anti-stickiness |
| _auto_escalate_to_agent() | `tools/verifier.py` | Post-tool escalation |
| _run_agent_with_timeout() | `orchestration/mode_router.py` | Agent dispatch |
| Action log (log_action) | `core/metrics.py` | Audit trail |
| Agent patterns (_AGENT_PATTERNS) | `llm/mode_classifier.py` | Pattern matching |
| Quick patterns (_QUICK_PATTERNS) | `llm/mode_classifier.py` | Pattern matching |

### brain_defs.py (1,681 lines) → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| build_tool_definitions() | `tools/schemas.py` | JSON schemas |
| _TOOL_ALIASES, _resolve_tool_name() | `tools/schemas.py` | Name resolution |
| _ARG_ALIASES, _normalize_tool_args() | `tools/schemas.py` | Arg normalization |
| _extract_tool_from_json(), _extract_single_tool() | `tools/schemas.py` | JSON extraction |
| _looks_like_json_garbage() | `tools/schemas.py` | Garbage detection |
| _parse_prompt_actions() | `tools/schemas.py` | Prompt parsing |
| _tools_as_prompt_text() | `tools/schemas.py` | Schema → text |
| _CORE_TOOL_NAMES, _build_core_tools() | `tools/registry.py` | Tool registration |
| _TERMINAL_BLOCKED, _TERMINAL_ADMIN_REQUIRED | `platform_impl/windows/terminal.py` | Safety data |
| _run_terminal() | `platform_impl/windows/terminal.py` | Terminal handler |
| _FILE_BLOCKED_DIRS | `platform_impl/windows/files.py` | Safety data |
| _manage_files() | `platform_impl/windows/files.py` | File handler |
| _manage_software() | `platform_impl/windows/terminal.py` | Winget handler |
| _toggle_system_setting() | `platform_impl/windows/settings.py` | Settings handler |
| VK_MEDIA_*, _press_media_key() | `platform_impl/windows/media.py` | Media keys |
| _open_spotify_app(), _wait_for_process() | `platform_impl/windows/media.py` | Spotify launch |
| _VERIFY_TOOLS, _APP_VERIFY, _verify_tool_completion() | `tools/verifier.py` | Completion checks |
| _execute_create_file() | `tools/executor.py` | File creation tool |

### speech.py (1,340 lines) → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| WhisperSTT class, listen() | `speech_new/stt.py` | Speech-to-text |
| speak(), _speak_piper/gtts/pyttsx3 | `speech_new/tts.py` | Text-to-speech |
| listen_for_wake_word(), wake variants | `speech_new/wakeword.py` | Wake word |
| speak_interruptible(), stop_speaking() | `speech_new/barge_in.py` | Interruptible speech |
| MicState enum, _mic_state, _is_speaking | `speech_new/audio_state.py` | State machine |

### desktop_agent.py (2,794 lines) → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| DesktopAgent.execute(), main loop | `automation/desktop_agent.py` | Orchestrator |
| _plan() | `automation/planner.py` | Step planning |
| _observe() | `automation/observer.py` | Screen observation |
| _verify_step(), _verify_goal() | `automation/verifier.py` | Verification |
| _diagnose(), retry logic | `automation/recovery.py` | Failure recovery |

### ai_providers.py (307 lines) → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| ChatProvider, Ollama/OpenAI/Anthropic/OpenRouter | `llm/provider_registry.py` | All providers |
| create_provider() factory | `llm/provider_registry.py` | Factory function |
| Rate limiting, health monitoring | `llm/provider_registry.py` | Per-provider |

### config.py (410 lines) → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Config loading, validation, first-run | `core/config_service.py` | Config management |
| Fernet encryption | `core/config_service.py` | Credential encryption |

### intent.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Keyword intent detection, patterns | `orchestration/fallback_router.py` | Offline fallback |

### actions.py → Split

| Old Code | New Module | Notes |
|----------|-----------|-------|
| open_app(), close_app(), minimize_app() | `platform_impl/windows/apps.py` | App management |
| google_search() | `features/web/service.py` | Web search |
| system commands (shutdown, restart, etc.) | `platform_impl/windows/terminal.py` | System commands |

### app_finder.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| AppFinder class (full) | `platform_impl/windows/apps.py` | App discovery |

### computer.py (927 lines) → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Computer class (full) | `platform_impl/windows/ui_automation.py` | Mouse/keyboard/screen |

### vision.py → Merge into automation/observer.py

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Screenshot, llava analysis, element finding | `automation/observer.py` | Vision + observation |

### web_agent.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| web_read(), web_search_extract() | `features/web/service.py` | Web reading/search |

### memory.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| MemoryStore, UserPreferences, HabitTracker | `features/memory/service.py` | All memory |

### reminders.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| ReminderManager (full) | `features/reminders/service.py` | All reminders |

### weather.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| get_weather(), get_forecast() | `features/weather/service.py` | All weather |

### news.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| get_news(), RSS parsing | `features/news/service.py` | All news |

### email_sender.py → Full

| Old Code | New Module | Notes |
|----------|-----------|-------|
| send_email(), credentials | `features/email/service.py` | All email |

### cognitive.py (1,153 lines) → Delete

| Old Code | New Module | Notes |
|----------|-----------|-------|
| 6-phase learning engine | *(removed)* | Mostly unused in production; useful parts fold into `llm/planner.py` |

### self_test.py → Keep (thin wrapper)

| Old Code | New Module | Notes |
|----------|-----------|-------|
| Runtime diagnostics | `tests/` + `self_test.py` wrapper | Tests migrate, self_test stays as CLI entry |

### smart_tester.py → Delete

| Old Code | New Module | Notes |
|----------|-----------|-------|
| LLM-driven test runner | *(removed)* | Incomplete, replaced by proper test suite |

---

## New Modules (no old source)

| New Module | Purpose |
|-----------|---------|
| `app/__init__.py` | Application package |
| `app/main.py` | Entry point (replaces main.py + run.py) |
| `app/bootstrap.py` | Dependency checking, Ollama setup |
| `app/container.py` | Dependency injection container |
| `app/lifecycle.py` | Startup/shutdown orchestration |
| `core/__init__.py` | Core package |
| `core/events.py` | Event bus (pub/sub between modules) |
| `core/state.py` | Shared runtime state (dataclasses) |
| `core/logging.py` | Structured logging setup |
| `core/metrics.py` | Performance metrics + action audit log |

---

## Migration Order (recommended)

1. **core/** — events, state, config_service, logging (no dependencies)
2. **tools/** — schemas, registry, safety_policy (depends on core/)
3. **llm/** — provider_registry, brain_service (depends on core/, tools/)
4. **platform_impl/windows/** — apps, terminal, files, settings, media (depends on tools/)
5. **speech_new/** — stt, tts, wakeword, barge_in, audio_state (depends on core/) — renamed to speech/ after root speech.py deleted
6. **features/** — weather, news, reminders, email, memory, web (depends on core/, tools/)
7. **automation/** — desktop_agent, planner, observer, verifier, recovery (depends on llm/, platform_impl/)
8. **orchestration/** — assistant_loop, session_manager, mode_router (depends on everything)
9. **app/** — main, bootstrap, container, lifecycle (wires everything together)
10. **tests/** — test suite (last, after all modules migrated)
