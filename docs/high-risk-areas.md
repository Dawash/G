# High-Risk Areas

**Date**: 2026-03-07
**Scope**: Root modules (25 files, 17,110 lines) + packages (12 packages, 4,456 lines)

---

## 1. CRITICAL Bug — `ai_providers.py:79`

```python
def chat(self, user_input):
    if is_rate_limited(self.provider_name):
        entry = _rate_limits.get(self.provider_name, {})   # <-- NameError!
```

`_rate_limits` is **undefined**. The module uses `_provider_state = _ProviderState()` (line 14) and the `is_rate_limited()` function delegates to `_provider_state.is_rate_limited()`. But the `.chat()` fallback path still references the old dict name. This will crash with `NameError` the first time any provider is rate-limited (HTTP 429).

**Fix**: Replace `_rate_limits.get(...)` with `_provider_state.rate_limits.get(...)`.

**Impact**: HIGH — any 429 response will crash the chat path instead of gracefully falling back.

---

## 2. Race Conditions (13 found)

### HIGH severity

| # | Location | Issue | Threads Involved |
|---|----------|-------|-----------------|
| 1 | `speech.py:58-60` | `get_mic_state()` reads `_mic_state` without `_mic_state_lock` | main loop + dashboard |
| 2 | `speech.py:47-48` | `_last_spoken_text` and `_speak_end_time` written by TTS thread, read by STT for echo detection — no lock | TTS thread + STT thread |
| 3 | `speech.py:467-473` | `set_next_speak_language()` writes `_next_speak_language` without lock, `speak()` reads it | main thread + any caller |
| 4 | `reminders.py` | `self.reminders` list modified by background checker thread (fire/reschedule) AND main thread (`add_reminder`, `list_active`, `snooze`) without synchronization | checker daemon + main thread |

### MEDIUM severity

| # | Location | Issue |
|---|----------|-------|
| 5 | `speech.py:282` | `_get_vad_model()` double-checked locking — first check outside lock, second inside. Python's GIL makes this mostly safe but it's fragile. |
| 6 | `speech.py:543` | Same pattern for `_get_whisper_model()` |
| 7 | `speech.py:920` | Same pattern for `_get_piper_voice()` |
| 8 | `brain.py:76` | `_brain_state.dynamic_tools` dict writes (from `create_tool`) have no lock; reads from `execute_dynamic_tool` could see partial state |
| 9 | `reminders.py:291` | `_running` flag read by checker thread, set by `stop()` — bool assignment is atomic in CPython but not guaranteed |
| 10 | `brain.py:459` | `_last_created_file` global written in `_execute_tool_inner`, read later — single-threaded in practice but unsafe by design |

### LOW severity

| # | Location | Issue |
|---|----------|-------|
| 11 | `ai_providers.py` | `_provider_state.rate_limits` dict modified by `_record_rate_limit`/`_clear_rate_limit`, read by `is_rate_limited` — no lock. GIL protects dict ops. |
| 12 | `ai_providers.py` | `_provider_state.ollama_available` bool set by health monitor thread, read by main thread — atomic in CPython |
| 13 | `app_finder.py:338` | `_app_index` global set once by background thread at startup, then read-only — safe in practice |

### Summary

- 4 HIGH: data corruption possible under load
- 6 MEDIUM: unlikely in practice due to GIL but architecturally unsound
- 3 LOW: safe under CPython's GIL, would break on other runtimes

---

## 3. Security / Trust Risks

### 3a. Command Injection via LLM Output

| Vector | Module | Line | Severity | Description |
|--------|--------|------|----------|-------------|
| `run_terminal` | brain_defs.py | 615 | **HIGH** | User voice -> LLM -> PowerShell `subprocess.run()`. Blocklist (`_TERMINAL_BLOCKED`) helps but is bypassable via encoding, aliases, or novel commands. |
| `exec()` in `create_tool` | brain.py | 104, 160 | **HIGH** | LLM-generated Python code executed via `exec()` with full `__builtins__`. No sandbox. |
| `manage_files` | brain_defs.py | 637+ | **MEDIUM** | File operations (move/copy/delete) on paths derived from LLM output. `_FILE_BLOCKED_DIRS` blocks system dirs but relative path traversal possible. |
| `manage_software` | brain_defs.py | 792+ | **MEDIUM** | `winget install/uninstall` with LLM-chosen package names. Could install malware if LLM is tricked. Confirmation required for uninstall but not install. |
| Desktop agent | desktop_agent.py | 1557 | **MEDIUM** | Agent can run arbitrary terminal commands via `_run_terminal_command()`. Safety check exists but only blocks a small set. |

### 3b. Credential Exposure

| Issue | Module | Severity |
|-------|--------|----------|
| Email password in plaintext JSON if `cryptography` unavailable | email_sender.py:55 | MEDIUM |
| API keys stored in config.json (Fernet encrypted, but key derived from machine ID — portable) | config.py | LOW |
| Ollama API has no auth — any local process can call it | ai_providers.py | INFO |

### 3c. Input Sanitization Gaps

| Issue | Module | Notes |
|-------|--------|-------|
| LLM tool arguments fed directly to system calls | brain.py, brain_defs.py | No type validation, no allowlist for expected values |
| `_toggle_system_setting` passes args to PowerShell scripts | brain_defs.py:1345+ | Long inline PowerShell with string interpolation |
| `google_search` passes query to URL | actions.py | Uses `quote_plus` — safe |
| `web_read` fetches arbitrary URLs from LLM | web_agent.py | No domain allowlist |

---

## 4. Mutable Module-Level Globals (35+)

### speech.py — WORST OFFENDER (11 mutable globals + 7 locks/events)

| Global | Line | Type | Protection | Writers |
|--------|------|------|------------|---------|
| `_mic_state` | 52 | str | `_mic_state_lock` (but getter skips it!) | `set_mic_state()` |
| `_vad_model` | 270 | model/None | `_vad_lock` | `_get_vad_model()` |
| `_vad_failed` | 270 | bool | `_vad_lock` | `_get_vad_model()` |
| `_whisper_model` | 502 | model/None | `_whisper_lock` | `_get_whisper_model()` |
| `_whisper_failed` | 502 | bool | `_whisper_lock` | `_get_whisper_model()` |
| `_piper_voice` | 875 | model/None | `_piper_lock` | `_get_piper_voice()` |
| `_piper_failed` | 875 | bool | `_piper_lock` | `_get_piper_voice()` |
| `_detected_language` | 452 | str | `_language_lock` | `_listen_whisper()` |
| `_next_speak_language` | 453 | str/None | **NONE** | `set_next_speak_language()` |
| `_last_spoken_text` | 47 | str | **NONE** | `speak()` |
| `_speak_end_time` | 48 | float | **NONE** | `speak()` |
| `_calibrated` | 603 | bool | **NONE** | `_calibrate_mic()` |
| `_pygame_initialized` | 818 | bool | `_pygame_lock` | `_init_pygame()` |
| `_stt_engine` | 1301 | str | **NONE** | `set_stt_engine()` |

### brain.py — 6 mutable globals

| Global | Line | Protection | Risk |
|--------|------|------------|------|
| `_brain_state` | 76 | `_state_lock` covers most fields | Safe for protected fields |
| `_brain_state.dynamic_tools` | 76 | **NONE** | MEDIUM — dict mutation without lock |
| `_last_created_file` | 459 | **NONE** | LOW — single-threaded path |
| `_CACHE_TTL` | 314 | Read-only after init | Safe |

### Other modules

| Module | Global | Protection | Risk |
|--------|--------|------------|------|
| `ai_providers.py` | `_provider_state` | **NONE** (dict/bool fields) | LOW — GIL |
| `cognitive.py` | `_cached_config` | **NONE** | LOW — set once |
| `cognitive.py` | `_db_lock` | N/A (is a lock itself) | Safe |
| `app_finder.py` | `_app_index` | **NONE** | LOW — set once at startup |
| `assistant.py` | `_runtime_state` | **NONE** (bool fields) | LOW — main thread only |
| `computer.py` | `_pyautogui` | **NONE** | LOW — lazy init once |

---

## 5. Giant Modules (Maintainability Risk)

| Module | Lines | Classes | Functions | Risk |
|--------|-------|---------|-----------|------|
| `desktop_agent.py` | 2,805 | 1 (DesktopAgent) | 1 (execute_desktop_task) | **HIGH** — one god-class with 40+ methods, impossible to unit test |
| `brain.py` | 2,426 | 1 (Brain) | 14 module functions | **HIGH** — tool execution, LLM calling, mode routing all entangled |
| `brain_defs.py` | 1,681 | 0 | 19 functions | **MEDIUM** — pure functions but very long, tool defs + handlers + aliases mixed |
| `speech.py` | 1,340 | 0 | 34 functions | **HIGH** — VAD + STT + TTS + wake word + barge-in in one file with 13 `global` statements |
| `cognitive.py` | 1,153 | 7 | 3 functions | **LOW** — well-structured classes, but mostly unused in production |
| `computer.py` | 932 | 0 | 18 functions | **LOW** — self-contained automation functions |

### Why this matters

- **Testing**: No function in `brain.py` or `desktop_agent.py` can be unit tested — all depend on LLM, subprocess, file I/O, or UI automation with no dependency injection.
- **Debugging**: A bug in `speech.py` requires understanding 13 interacting global variables and 7 locks.
- **Code review**: A change to tool execution in `brain.py` requires reading 2,400 lines of context.

---

## 6. Performance Risks

| Area | Latency | Root Cause | Impact |
|------|---------|------------|--------|
| Ollama cold call | 3-8s | Model loading into VRAM after idle | First request after wake is very slow |
| Ollama warm with tools | 6.5s avg | 18-tool schema adds ~6s vs 0.4s without tools | Every tool-calling request pays this cost |
| Piper TTS cold load | 5.8s | ONNX model loading | First spoken response is very delayed |
| Whisper cold load | 1.5s | CUDA model loading | First STT after startup |
| Desktop agent per-step | 2-4s | llava vision model inference per observe() | 10 steps = 20-40s total |
| Spotify play_music | 5-8s | Sequential time.sleep() calls (1.5+0.3+0.5+0.1+3.5) | Feels unresponsive |
| Bluetooth toggle | 3-5s | Massive inline PowerShell WinRT script | Could just open Settings |
| `_toggle_system_setting` | 1-5s | PowerShell subprocess for every toggle | Could use ctypes for some |

### Blocking points in critical path

```
User speaks → [VAD 0.3s] → [Whisper 0.5-1.5s] → [LLM 0.4-6.5s] → [tool 0.1-5s] → [TTS 0.3-5.8s]
                                                                                        Total: 1.6-19.6s
```

The dominant bottleneck is **Ollama with tools (6.5s avg)**. Without tools (quick_chat), it's 0.4s. This suggests the 18-tool schema is too large for qwen2.5:7b to process efficiently.

---

## 7. Architectural Risks

### 7a. Function-attribute state passing (`getattr`/`setattr` on functions)

```python
# brain.py:239 — sets attributes on execute_tool function
execute_tool._last_user_input = user_input
execute_tool._brain_quick_chat = self.quick_chat
```

This anti-pattern makes `execute_tool` non-reentrant and impossible to test in isolation. If two threads call `think()` concurrently, they'll overwrite each other's function attributes.

### 7b. Circular import risk

`brain.py` → `brain_defs.py` → `brain.py` (via `execute_tool` passed as argument)
`brain.py` → `desktop_agent.py` → `brain.py` (via action_registry)
`speech.py` ← `assistant.py` → `brain.py` → `speech.py` (via speak_fn callbacks)

Currently avoided by late imports (`from computer import ...` inside functions), but fragile.

### 7c. No dependency injection

All modules use module-level globals and direct imports. This means:
- Cannot swap implementations for testing
- Cannot run multiple Brain instances
- Cannot mock speech for headless testing
- State leaks between test runs

### 7d. No error boundaries

A crash in any tool handler (e.g., `_manage_files` throws `PermissionError`) propagates up through `execute_tool` → `_think_native` → `think()` → `assistant.py` main loop. The main loop catches `Exception` but the error message returned to the user is often the raw Python traceback via `f"Error: {e}"`.

---

## 8. Product/UX Risks

| Risk | Impact | Description |
|------|--------|-------------|
| 6.5s tool-calling latency | HIGH | Users wait 6.5s just for LLM to pick a tool. Most voice assistants respond in <2s. |
| No feedback during LLM thinking | MEDIUM | 2s timer says "Working on it..." but only if LLM takes >2s. No progress for tool execution. |
| Barge-in misses short words | MEDIUM | 0.5s polling interval means "stop" might be missed if said between polls |
| Echo detection is fragile | MEDIUM | Compares STT output to `_last_spoken_text` — can fail if Whisper transcribes TTS differently |
| Agent mode is silent | LOW | Good for power users but confusing for first-time users who don't see what's happening |
| No graceful degradation | MEDIUM | If Ollama is down, falls back to keyword intent — but many commands (run_terminal, manage_files) have no keyword fallback |

---

## 9. TODO: Priority Fix Order

1. **CRITICAL**: Fix `ai_providers.py:79` — `_rate_limits` → `_provider_state.rate_limits`
2. **HIGH**: Add lock to `reminders.py` for `self.reminders` list access
3. **HIGH**: Add lock protection for `_last_spoken_text` / `_speak_end_time` in speech.py
4. **HIGH**: Validate `run_terminal` arguments more strictly (not just blocklist)
5. **MEDIUM**: Sandbox `exec()` in `create_tool` — restrict `__builtins__`
6. **MEDIUM**: Fix `get_mic_state()` to use `_mic_state_lock`
7. **MEDIUM**: Add lock for `set_next_speak_language()`
8. **MEDIUM**: Reduce Ollama tool schema size (group tools, use fewer parameters)
9. **LOW**: Extract function-attribute state passing into explicit parameter objects
10. **LOW**: Add type validation for LLM tool arguments before execution
