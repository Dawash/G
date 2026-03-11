# Global State Migration Plan

**Date**: 2026-03-07
**Goal**: Move all mutable module-level globals into `core/state.py` dataclasses or dedicated services, accessed through `app/container.py`.

## Migration Status Legend

- **DONE** = Migrated to core/state.py or a dedicated service, old code updated
- **ALIASED** = State object exists in core/state.py, old module creates instance + legacy aliases
- **PLANNED** = Target identified, not yet migrated
- **SKIP** = Not worth migrating (immutable, constant, or set-once-at-import)

---

## assistant.py (7 globals)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_runtime_state` | 51 | `RuntimeState` | `app/container.py` → `container.state` | P1 | ALIASED — creates own instance, not from container |
| `trigger_emergency_stop()` | 53 | function | `core/control_flags.py` | P1 | **DONE** — re-exports from control_flags |
| `clear_emergency_stop()` | 70 | function | `core/control_flags.py` | P1 | **DONE** — re-exports from control_flags |
| `is_emergency_stopped()` | 74 | function | `core/control_flags.py` | P1 | **DONE** — re-exports from control_flags |
| `_start_hotkey_listener()` | 78 | function | `core/control_flags.py` | P1 | **DONE** — re-exports from control_flags |
| `_ollama_was_down` | 171 | `list[bool]` | `ProviderState.ollama_available` | P3 | PLANNED — local to `run()`, low risk |
| `is_connected` | 192 | bool | `SessionState` | P3 | PLANNED — local to `run()`, low risk |

---

## brain.py (8 globals)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_brain_state` | 76 | `BrainState` | `app/container.py` → `container.state.brain` | P1 | ALIASED — creates own instance |
| `_undo_stack` | 302 | list (alias) | `_brain_state.undo_stack` | P2 | ALIASED — `= _brain_state.undo_stack` |
| `_recent_actions` | 303 | list (alias) | `_brain_state.recent_actions` | P2 | ALIASED — `= _brain_state.recent_actions` |
| `_state_lock` | 304 | Lock (alias) | `_brain_state._lock` | P2 | ALIASED — `= _brain_state._lock` |
| `_response_cache` | 305 | dict (alias) | `_brain_state.response_cache` | P2 | ALIASED — `= _brain_state.response_cache` |
| `_last_created_file` | 309 | str/None | `BrainState.last_created_file` | P2 | PLANNED — has TODO comment |
| `_experience_learner` | 312 | object/None | `BrainState.experience_learner` | P3 | PLANNED — has TODO comment |
| `_CACHE_TTL` | 314 | dict (constant) | N/A | — | SKIP — immutable after init |
| `_MAX_ESCALATION_DEPTH` | 787 | int (constant) | N/A | — | SKIP — immutable constant |

---

## speech.py (18 mutable globals + 7 locks/events)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_engine` | 21 | pyttsx3 engine | `AudioState` or lazy init in TTS service | P3 | PLANNED |
| `_recognizer` | 22 | sr.Recognizer | STT service internal state | P3 | PLANNED |
| `_calibrated` | 32 | bool | `AudioState.calibrated` | P3 | PLANNED |
| `_input_mode` | 35 | str | `AudioState.input_mode` | P3 | PLANNED |
| `_tts_lock` | 38 | Lock | TTS service internal lock | P3 | PLANNED |
| `_stop_speaking` | 41 | Event | TTS service internal event | P3 | PLANNED |
| `_is_speaking` | 44 | Event | `AudioState.is_speaking` | P2 | PLANNED — race condition (HIGH) |
| `_last_spoken_text` | 47 | str | `AudioState.last_spoken_text` | P2 | PLANNED — race condition (HIGH) |
| `_speak_end_time` | 48 | float | `AudioState.speak_end_time` | P2 | PLANNED — race condition (HIGH) |
| `_mic_state` | 54 | str | `AudioState.mic_state` | P2 | PLANNED — race condition (HIGH, getter skips lock) |
| `_mic_state_lock` | 55 | Lock | `AudioState._lock` | P2 | PLANNED |
| `_wake_words` | 74 | set | `AudioState.wake_words` | P3 | PLANNED |
| `_vad_model` | 268 | model/None | STT service internal state | P3 | PLANNED |
| `_vad_lock` | 269 | Lock | STT service internal lock | P3 | PLANNED |
| `_vad_failed` | 270 | bool | STT service internal state | P3 | PLANNED |
| `_detected_language` | 450 | str | `AudioState.detected_language` | P2 | PLANNED |
| `_language_lock` | 451 | Lock | `AudioState._lock` | P2 | PLANNED |
| `_next_speak_language` | 452 | str/None | `AudioState.next_speak_language` | P2 | PLANNED — race condition (HIGH, no lock) |
| `_whisper_model` | 500 | model/None | STT service internal state | P3 | PLANNED |
| `_whisper_lock` | 501 | Lock | STT service internal lock | P3 | PLANNED |
| `_whisper_failed` | 502 | bool | STT service internal state | P3 | PLANNED |
| `_pygame_initialized` | 816 | bool | TTS service internal state | P4 | PLANNED |
| `_pygame_lock` | 817 | Lock | TTS service internal lock | P4 | PLANNED |
| `_piper_voice` | 873 | model/None | TTS service internal state | P3 | PLANNED |
| `_piper_lock` | 874 | Lock | TTS service internal lock | P3 | PLANNED |
| `_piper_failed` | 875 | bool | TTS service internal state | P3 | PLANNED |
| `_stt_engine` | 1301 | str | `AudioState.stt_engine` | P3 | PLANNED |

---

## ai_providers.py (1 global)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_provider_state` | 14 | `ProviderState` | `app/container.py` → `container.state.provider` | P1 | ALIASED — creates own instance |

**Bug fixed this phase**: Line 79 referenced undefined `_rate_limits` — changed to `_provider_state.rate_limits`.

---

## app_finder.py (1 global)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_app_index` | 295 | dict/None | `container.app_finder` service internal cache | P4 | PLANNED — set once at startup, low risk |

---

## cognitive.py (2 globals)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `_db_lock` | 33 | Lock | Service internal lock | P4 | PLANNED — used only within cognitive.py |
| `_cached_config` | 44 | dict/None | Service internal cache | P4 | PLANNED — set once, low risk |

---

## desktop_agent.py (1 class-level mutable)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `DesktopAgent._active_instance` | 126 | instance/None | `AgentState` or `control_flags.on_stop()` callback | P2 | PLANNED — used for emergency stop cancel |

---

## reminders.py (thread-safety issue)

| Global | Line | Type | Target | Priority | Status |
|--------|------|------|--------|----------|--------|
| `self.reminders` | — | list | Needs Lock protection | P1 | PLANNED — race condition (HIGH, 2 threads modify) |
| `self._running` | — | bool | Needs Lock or Event | P3 | PLANNED |

---

## Migration Priority Order

### P1 — Critical (do first, fix bugs and races)
1. ~~Emergency stop → `core/control_flags.py`~~ **DONE**
2. ~~`ai_providers.py:79` bug fix~~ **DONE**
3. `reminders.py` — add Lock to `self.reminders` list access
4. Wire `_runtime_state` and `_brain_state` and `_provider_state` through container (single shared instances instead of 3 independent ones)

### P2 — High (fix race conditions)
5. `speech.py` — protect `_last_spoken_text`, `_speak_end_time` with lock
6. `speech.py` — fix `get_mic_state()` to use `_mic_state_lock`
7. `speech.py` — protect `_next_speak_language` with `_language_lock`
8. `brain.py` — replace `_last_created_file` and `_experience_learner` direct globals with `_brain_state` fields
9. `brain.py` — remove legacy aliases (`_undo_stack`, `_recent_actions`, etc.) once all internal uses updated

### P3 — Medium (clean up state management)
10. `speech.py` — move model globals (`_vad_model`, `_whisper_model`, `_piper_voice`) into service classes
11. `speech.py` — move configuration globals (`_input_mode`, `_stt_engine`, `_calibrated`) into `AudioState`
12. `assistant.py` — move `_ollama_was_down` into `ProviderState`

### P4 — Low (cosmetic, low risk)
13. `app_finder.py` — encapsulate `_app_index` in a class
14. `cognitive.py` — encapsulate `_cached_config` in class
15. `speech.py` — move pygame init state into TTS service

---

## How Container Wiring Will Work

Currently each module creates its own state instance:
```python
# assistant.py
_runtime_state = _RuntimeState()

# brain.py
_brain_state = _BrainState()

# ai_providers.py
_provider_state = _ProviderState()
```

Target: a single `RuntimeState` created in the container, shared across all modules:
```python
# app/bootstrap.py (future)
container = Container()  # creates RuntimeState with all sub-states

# Modules receive state via parameter or import from container
# Option A: constructor injection
brain = BrainService(state=container.state.brain)

# Option B: module-level reference (transitional)
import app.container as _container
_brain_state = _container.instance.state.brain
```

Option B is the transitional approach — it lets us wire shared state without rewriting module signatures. Option A is the target for new code.
