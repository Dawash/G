# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Voice-first personal AI operating system for Windows. Listens continuously via microphone (with wake word detection), understands intent using AI, executes system/web actions, and speaks responses naturally. Primary provider is **Ollama** (local, qwen2.5:7b). Also supports **OpenAI, Anthropic, and OpenRouter** APIs. Features wake word detection, conversation mode with auto-sleep, meta-commands (undo, repeat, shorter, correction), context awareness, topic tracking, intelligent app discovery, desktop automation (agentic mode), persistent memory with personalization, speech barge-in, reminders, weather, news, email, web reading, vision, and more.

## Running the Assistant

```bash
python run.py
```

The launcher (`run.py`) auto-checks Python, installs missing dependencies, validates all modules, sets up Ollama if needed, then starts the assistant. First run prompts for username, AI name, API provider choice, and API key (saved to `config.json`).

Alternative: `pip install -r requirements.txt && python main.py` (manual dep install).

Legacy: `python main3.py` (moved to `legacy/` folder).

## Architecture — 21 Modules

```
run.py              → Auto-launcher (deps, Ollama setup, validation, launch)
main.py             → Entry point (minimal)
assistant.py        → Main loop, state machine (IDLE/ACTIVE), wake word, meta-commands, undo, crash recovery
brain.py            → LLM Brain with 15 core tools, undo registry, topic tracking, context awareness, dynamic tool factory
config.py           → Config management, first-run setup, multi-provider selection
ai_providers.py     → Ollama / OpenAI / Anthropic / OpenRouter providers + rate limits
speech.py           → Silero VAD + Whisper STT (GPU) + Piper/pyttsx3/gTTS TTS + wake word + noise filtering + barge-in
intent.py           → Keyword intent detection, 21 types, multi-action splitting
actions.py          → App launch, window mgmt, system cmds, web search
app_finder.py       → Registry + Start Menu + fuzzy match + web shortcuts + similar app suggestions
desktop_agent.py    → Agentic Mode: plan→observe→think→act→verify→diagnose loop
vision.py           → Screenshot capture, llava vision analysis, element finding
computer.py         → Low-level mouse/keyboard/screen control (pyautogui wrapper)
web_agent.py        → Web reading, DuckDuckGo search, deep research, content extraction
email_sender.py     → SMTP email sending with credential storage
memory.py           → SQLite persistent memory, preferences, nicknames, habit tracking, routine detection, app category defaults
cognitive.py        → Cognitive engine: learning, comprehension, problem solving, decision making, creativity, autonomy
weather.py          → Open-Meteo API: current conditions, forecast, rain alerts
reminders.py        → NLP time parsing, recurring, background checker
alarms.py           → Morning alarm system: sound playback, voice dismiss, LLM motivation, weather+news briefing
news.py             → Google News RSS, multi-category, BBC fallback
user_choice.py      → Interactive multi-choice system: present options, parse voice/keyboard input
self_test.py        → Runtime diagnostics: 17 tests across core subsystems
legacy/             → Archived files (main3.py, openclaw_bridge.py)
```

### Data Flow — Brain-First Architecture with State Machine

```
IDLE state:
  speech.listen_for_wake_word() [Silero VAD + Whisper, short 2s clips]
    → Wake word detected? → switch to ACTIVE, greet user

ACTIVE state:
  Microphone → speech.listen() [Silero VAD + Whisper STT, GPU-accelerated]
    → Noise filter: discard filler words, punctuation-only, too-short
    → Meta-commands? → skip/shorter/repeat/undo/correction (no Brain needed)
    → Quick checks: exit / disconnect / connect (no API needed)
    → Brain available (Ollama running + not rate-limited)?
        YES → brain.think() → mode routing (quick/agent/research)
              → tools execute → undo registered → results fed back → spoken response
        NO  → intent.detect() (offline keyword fallback)
              → action_map handler
    → speech.speak_interruptible() [barge-in enabled]
    → memory.log_event()
    → 90s inactivity? → switch to IDLE, "Going to sleep"
```

The Brain uses native tool calling (Ollama qwen2.5:7b) to let the LLM decide which system actions to take. 3-tier fallback: native tool calls → JSON extraction from text → prompt-based mode. Mode-based routing: quick (80%, direct tool calling), agent (15%, desktop automation), research (5%, multi-source web research). Topic tracking adjusts context window size for multi-turn conversations on the same subject.

### AI Provider System (ai_providers.py)

- `ChatProvider` base class with `chat()` method and sliding context window (last 20 messages)
- `OllamaProvider` — local llama3.1 via Ollama (primary, free)
- `OpenAIProvider` — gpt-4o-mini via OpenAI
- `AnthropicProvider` — Claude Sonnet via Anthropic Messages API
- `OpenRouterProvider` — configurable model via OpenRouter
- `create_provider(name, key, prompt)` factory function
- Per-provider rate limiting (each provider tracks its own 429 backoff independently)
- Ollama health monitoring with periodic reconnection (60s check interval)
- Automatic offline fallback to cached `responses.json` on API failure
- Dead-key detection: returns None on insufficient_quota, falls through to keyword fallback

### LLM Brain (brain.py)

The intelligent core — 18 core tools for Ollama, full set for cloud providers:

**Core OS** (11): `open_app`, `close_app`, `minimize_app`, `google_search`, `get_weather`, `get_forecast`, `get_time`, `get_news`, `set_reminder`, `list_reminders`, `system_command`

**System Tools** (3): `run_terminal`, `manage_files`, `manage_software`

**Desktop Automation** (6): `search_in_app`, `type_text`, `press_key`, `click_at`, `scroll`, `agent_task`

**Vision** (2): `take_screenshot`, `find_on_screen`

**Web & Communication** (3): `web_read`, `web_search_answer`, `send_email`

**Self-Management** (3): `create_file`, `run_self_test`, `restart_assistant`

**Interactive** (3): `ask_user_choice`, `ask_user_input`, `ask_yes_no`

**Meta Brain** (5): `spawn_agents`, `chain_tasks`, `create_tool`, `analyze_and_improve`, `reason_deeply`

Key features:
- 3-tier tool calling: native → JSON extraction → prompt-based fallback
- Ollama gets 15 core tools (qwen2.5:7b handles well)
- 90+ fuzzy aliases for tool name extraction from LLM output
- Response sanitizer strips llama special tokens and markdown artifacts
- `quick_chat()` for lightweight no-tools LLM responses (always-LLM mode)
- Mode-based routing: quick/agent/research classification before LLM sees request
- Dynamic tool registry: brain can create new tools at runtime via `create_tool`
- Undo registry: reversible actions (open/close app, toggle settings) with 30s window
- Topic tracking: same-topic conversations get larger context windows (6→12 messages)
- Context awareness: ambient context injection (active window, clipboard on trigger words, time-of-day)
- Recent actions buffer: "do that again" replays last tool call
- Failure recovery: suggests similar apps when "not found"

### Desktop Agent — Agentic Mode (desktop_agent.py)

Proactive autonomous desktop automation with plan-first architecture:

```
execute(goal):
    1. print("Thinking...") — silent planning, no speech
    2. _plan(goal) → LLM generates step-by-step plan (max 10 steps)
    3. For each planned step:
        a. _observe(screen) → screenshot + llava vision + OS window info
        b. _think(goal, screen, plan_step) → decide exact tool + args
        c. _act(decision) → execute tool (or run_command for terminal)
        d. _verify_step() → check via tool result + vision + web extraction
        e. If failed → _diagnose() → multi-round LLM fix → retry
    4. Auto-completion detection from tool results
```

- **Silent execution**: agent thinks in console only, speaks only final result
- **Web verification**: extracts browser URL and page content to verify web actions
- **Terminal commands**: `run_command` tool for system checks (tasklist, systeminfo)
- **Diagnosis**: multi-round LLM consultation on failures with specific fix actions
- **Stuck detection**: detects repeated failures and forces alternative approaches
- **Sub-agents**: can split independent subtasks for sequential execution
- **Vision**: uses llava to understand screen state, detect blockers, find elements
- **OpenClaw integration**: optional advanced browser/messaging tools

### Speech System (speech.py)

**Input — Silero VAD + Whisper STT**:
- Silero VAD for neural voice activity detection (512-sample chunks at 16kHz)
- faster-whisper with GPU acceleration (RTX 4060 CUDA, float16, beam_size=1)
- Local model stored in `models/whisper-base/` (loads in 0.4s)
- Auto-detects language (English, Hindi, Nepali, etc.)
- Noise filtering: discards filler words, punctuation-only, too-short utterances
- Mic state tracking: IDLE/LISTENING/PROCESSING/SPEAKING
- Echo suppression: `_is_speaking` flag prevents self-listening during TTS

**Wake Word Detection**:
- `listen_for_wake_word()` — blocks until wake word, uses short 2s VAD clips
- Wake words auto-generated from AI name with common Whisper mishearings
- Fuzzy matching (SequenceMatcher, threshold 0.6)

**Output — TTS**:
- English: Piper (neural, offline) → pyttsx3 fallback
- Hindi/Nepali/other: gTTS (online, natural quality)

**Barge-in**:
- `speak_interruptible()` — speaks in background while monitoring mic
- Sentence-level splitting — can stop between sentences
- `stop_speaking()` — immediately halts TTS

### Always-LLM Responses (assistant.py)

Every response is a fresh LLM-generated sentence — never canned text:
- Exit/goodbye → `brain.quick_chat()` generates unique farewell
- Disconnect/connect → fresh acknowledgment each time
- Dead key warning → natural explanation
- Chat fallback → personalized response
- Provider switch → new brain greets user
- All special commands (self-test, guardian, etc.) → LLM-generated acknowledgment

### Intent System (intent.py)

Keyword-based offline fallback (used when Brain is unavailable):
- Pattern matching — regex for natural speech ("can you open Steam")
- Multi-action splitting — handles compound commands
- 18 intent types: `quit`, `disconnect`, `connect`, `switch_provider`, `shutdown`, `restart`, `cancel_shutdown`, `sleep`, `google_search`, `open_app`, `close_app`, `minimize_app`, `weather`, `forecast`, `time`, `news`, `set_reminder`, `list_reminders`, `snooze`, `chat`

### State Machine & Meta-Commands (assistant.py)

Conversation mode with wake word detection:
- **IDLE state**: `listen_for_wake_word()` blocks until wake word (Silero VAD + Whisper, fuzzy match)
- **ACTIVE state**: Normal listen/think/speak loop, auto-sleeps after 90s inactivity
- **Meta-commands**: "skip" (stop speech), "shorter" (summarize), "repeat", "undo" (30s window), "more detail"
- **Correction detection**: "No, I said Chrome not Notepad" → re-processes corrected text
- **Instant acknowledgment**: 2s timer says "Working on it..." for slow brain responses
- **Daily briefing**: time-appropriate greeting + weather + rain + battery + reminders

### Personalization (memory.py)

- **Nicknames**: `set_nickname("my browser", "Firefox")` + `resolve_nickname(text)`
- **Response preferences**: tracks shorter/longer preferences, returns preferred length
- **Routine detection**: finds repeated commands at specific hours/days
- **Proactive suggestions**: "You usually open Spotify around now"

### Vision System (vision.py)

- Screenshot capture via Pillow
- llava model for screen understanding
- Element finding by description
- Active window title detection
- YES/NO parsing for verification/blocker detection

### Web Agent (web_agent.py)

- `web_read(url)` — fetch and extract readable text from web pages
- `web_search_extract(query)` — DuckDuckGo + Wikipedia multi-source search
- Used by desktop agent for web verification

### Memory System (memory.py)

SQLite-backed persistent memory:
- `MemoryStore` — long-term facts, session events, usage logging
- `UserPreferences` — learned preferences (favorite apps, common searches)
- `HabitTracker` — temporal usage patterns, proactive suggestions

### Reminders (reminders.py)

- Natural language time parsing ("5pm", "in 30 minutes", "every Monday at 9am")
- Recurring reminders (daily, weekly, weekdays)
- Background checker thread (30s interval)
- Snooze support, persistent JSON storage

### Weather (weather.py)

- Open-Meteo API (free, no API key)
- Auto location detection via IP
- Current conditions + hourly forecast + rain alerts

### News (news.py)

- Google News RSS feeds (no API key)
- Categories: general, tech, sports, entertainment, science, business, health
- BBC/CNN fallback, 1-hour cache

## Generated Files (not in repo)

- `config.json` — username, AI name, provider, encrypted API key, ollama_model
- `responses.json` — conversation history for offline fallback
- `memory.db` — SQLite persistent memory
- `reminders.json` — active reminders
- `app_cache.json` — discovered app index
- `news_cache.json` — cached headlines
- `email_creds.json` — SMTP credentials (encrypted password)
- `assistant.log` — runtime log (rotating, max 5MB)
- `models/whisper-base/` — local Whisper model files

## Feature Health

| Feature | Status |
|---------|--------|
| Voice listening loop | ✅ Silero VAD + Whisper STT, GPU, multilingual |
| Wake word detection | ✅ Silero VAD + Whisper + fuzzy match, configurable AI name |
| Conversation mode | ✅ IDLE/ACTIVE state machine, 90s auto-sleep |
| TTS responses | ✅ Piper (English) + gTTS (Hindi/Nepali) + pyttsx3 fallback |
| Speech barge-in | ✅ Interrupt mid-sentence, process new input |
| Noise filtering | ✅ Filters filler words, punctuation-only, too-short utterances |
| Meta-commands | ✅ skip, shorter, repeat, undo, more detail, correction |
| Undo | ✅ Reverses open/close/toggle within 30s window |
| Context awareness | ✅ Active window, clipboard (on trigger), time-of-day |
| Topic tracking | ✅ Dynamic context window (6→12) for same-topic conversations |
| "Do that again" | ✅ Replays last tool call |
| Always-LLM responses | ✅ Every response is fresh LLM-generated |
| Multi-API chat (Ollama/OpenAI/Anthropic/OpenRouter) | ✅ Sliding context window |
| LLM Brain (15 core tools) | ✅ 3-tier tool calling, mode-based routing |
| Desktop agent (agentic mode) | ✅ Plan→observe→think→act→verify→diagnose |
| Vision (llava) | ✅ Screenshot analysis, element finding |
| Web agent | ✅ Page reading, deep research, DuckDuckGo + Wikipedia |
| Smart app discovery | ✅ Registry + Start Menu + fuzzy match + similar suggestions |
| Instant acknowledgment | ✅ "Working on it..." after 2s for slow responses |
| Daily briefing | ✅ Time-aware greeting + weather + rain + battery + reminders |
| Weather (Open-Meteo) | ✅ Auto-location, city support, forecast, rain alerts |
| News briefing (RSS) | ✅ Multi-category, startup briefing |
| Reminders | ✅ NLP parsing, recurring, background alerts |
| Persistent memory | ✅ SQLite: preferences, nicknames, habits, routines |
| Email sending | ✅ SMTP with credential storage |
| Dynamic tool creation | ✅ Brain creates new tools at runtime |
| Crash recovery | ✅ Main loop catches exceptions, keeps running |
| Self-test diagnostics | ✅ 16 tests across core subsystems |
| Dashboard | ✅ PyQt6 + QWebEngine, mic state indicator, action log |
| Encrypted credentials | ✅ Fernet encryption (machine-key derived), auto-migration |
| Dynamic voice tone | ❌ Not implemented |
| Cross-platform (Linux/macOS) | ❌ Windows-only currently |

## Development Priorities

1. **Runtime stability** — must not crash during normal use
2. **Speech UX** — barge-in ✅, wake word ✅, VAD ✅, latency ✅
3. **Desktop agent reliability** — plan accuracy, recovery, web verification
4. **Core daily features** — weather ✅, reminders ✅, news ✅, undo ✅
5. **Memory & personalization** — preferences ✅, nicknames ✅, routines ✅
6. **Performance** — startup speed, idle CPU, non-blocking operations
7. **Offline capability** — local STT ✅, local LLM ✅ (Ollama)
8. **Security** — encrypted credential storage, input sanitization

## Key Constraints

- Windows-only (pygetwindow, Windows registry, system commands)
- Requires microphone and speakers
- Ollama must be installed and running for primary provider
- Requires internet for Google STT fallback and web features
- Python 3.12 recommended (also works with 3.14)
- No formal test suite (has self_test.py for runtime diagnostics)

## Autonomous Development Mode

When improving this project, follow the cycle:

1. Plan changes
2. Implement
3. Run `python run.py` and verify
4. Fix any failures
5. Never leave the system in a broken state

Use parallel agents for independent tasks. Always verify by execution, not assumption.

## Non-Negotiable Safety Rules

- Never log secrets (API keys, tokens, private user data)
- Never execute destructive commands without explicit confirmation
- Desktop agent thinks silently — no announcing plans
- Always provide emergency stop for agentic mode
- Always keep structured audit trail for actions and errors
