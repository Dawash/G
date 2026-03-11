# Current System Baseline

**Date**: 2026-03-07 (updated)
**Version**: Post brain.py split + partial package extraction
**Total codebase**: ~21,566 lines across 107 .py files (25 root + 82 in 12 packages)

---

## 0. Package Inventory

In addition to the 25 root modules, prior refactor phases extracted code into packages.
These packages are **not yet wired into production** — the root modules remain the live code.

| Package | Files | Lines | Purpose |
|---------|-------|-------|---------|
| `llm/` | 8 | 400 | Mode classifier, prompt builder (extracted from brain.py) |
| `orchestration/` | 7 | 566 | Command router, session manager, response dispatcher, fallback |
| `tools/` | 8 | 216 | Safety policy, tool registry stubs |
| `platform_impl/` | 8 | 324 | Windows media, settings (extracted from brain_defs.py) |
| `core/` | 6 | 552 | Centralized state (BrainState, AudioState, ProviderState), EventBus, DI container |
| `app/` | 5 | 153 | App container, bootstrap stubs |
| `automation/` | 6 | 129 | Agent stubs |
| `features/` | 13 | 174 | Feature stubs (weather, news, reminders, email, memory, web) |
| `speech_new/` | 6 | 121 | Speech subsystem stubs |
| `dashboard/` | 7 | 1,390 | PyQt6 + QWebEngine dashboard UI |
| `tests/` | 5 | 64 | Test stubs |
| `legacy/` | 2 | — | Archived files (main3.py, openclaw_bridge.py) |
| **Total packages** | **82** | **4,456** | |

**Root modules (production code)**: 25 files, 17,110 lines

---

## 1. Module Responsibilities (Root — Production)

| Module | Lines | Responsibility | State? | Threads? |
|--------|-------|---------------|--------|----------|
| `run.py` | 597 | Launcher: deps, Ollama setup, validation, launch | No | No |
| `main.py` | 20 | Entry point → `assistant.run()` | No | No |
| `assistant.py` | 916 | **Main loop**: state machine (IDLE/ACTIVE), meta-commands, wake word, undo, greeting | Yes (6 globals) | Yes (4+) |
| `brain.py` | 2,953 | **LLM Brain**: tool calling, mode routing, undo, caching, cognitive integration | Yes (10 globals) | Yes (Lock + ThreadPool) |
| `brain_defs.py` | 1,681 | Tool definitions, aliases, JSON extraction, pure handlers | No | No |
| `config.py` | 411 | Config load/save, encryption, first-run setup | No | No |
| `ai_providers.py` | 308 | Multi-provider LLM chat, rate limiting, health checks | Yes (3 globals) | No |
| `speech.py` | 1,339 | VAD + Whisper STT + Piper/gTTS TTS + wake word + barge-in | Yes (15+ globals) | Yes (9 Locks) |
| `intent.py` | 453 | Keyword fallback intent detection | No | No |
| `actions.py` | 184 | System commands, window management | No | No |
| `app_finder.py` | 517 | Registry + Start Menu scan, fuzzy match, launch | Yes (1 global cache) | No |
| `desktop_agent.py` | 2,794 | Autonomous desktop automation (plan→observe→think→act→verify) | Yes (instance state) | Yes (ThreadPool) |
| `vision.py` | 596 | Screenshot capture + llava analysis | No | No |
| `computer.py` | 927 | Keyboard/mouse/UI automation, accessibility tree | No | No |
| `web_agent.py` | 535 | Web reading, DuckDuckGo + Wikipedia search, deep research | No | No |
| `email_sender.py` | 161 | SMTP email sending | No | No |
| `memory.py` | 379 | SQLite persistent memory, preferences, habits | Yes (Lock) | No |
| `cognitive.py` | 1,153 | 6-phase learning engine | Yes (Lock, global cache) | No |
| `weather.py` | 379 | Open-Meteo API: current, forecast, rain alerts | No | No |
| `reminders.py` | 386 | NLP time parsing, recurring reminders, background checker | Yes (thread, queue) | Yes (daemon) |
| `news.py` | 253 | Google News RSS, multi-category | No | No |
| `self_test.py` | 247 | Runtime diagnostics (13 tests) | No | Yes (ThreadPool) |
| `smart_tester.py` | 664 | LLM-driven live test runner | Yes (subprocess) | Yes (reader thread) |

---

## 2. Call Graphs

### 2.1 Startup Flow

```
run.py::main()
├── _relaunch_with_preferred_python()     # py -3.12 relay if needed
├── check_python()                        # verify >= 3.10
├── check_dependencies()                  # pip install missing packages
│   └── install_package(pip_name)         # subprocess pip call
├── setup_ollama()                        # ensure Ollama + model ready
│   ├── _ollama_is_installed()            # PATH + Program Files check
│   ├── _ollama_is_running()              # HTTP GET localhost:11434
│   ├── _start_ollama()                   # subprocess.Popen("ollama serve")
│   ├── _ollama_has_model(model)          # /api/tags endpoint
│   └── _pull_model(model)               # subprocess "ollama pull"
├── validate_provider()                   # test API key (401/429 detection)
│   └── _check_vision_model()             # check llava availability
├── check_modules()                       # importlib.import_module() × 21
└── launch()                              # subprocess or direct import
    └── main.py → assistant.run()
```

**Total startup time**: ~5-15s (depends on Ollama warm state, network for deps)

### 2.2 Assistant Main Loop (`assistant.py::run()`)

```
run()
├── load_config()                                # config.json → dict
├── create_provider(name, key, prompt, model)     # OllamaProvider instance
├── Brain(provider, key, username, ...)           # LLM brain init
│   ├── build_tool_definitions()                  # 29 tool schemas
│   ├── _build_core_tools()                       # 18-tool subset for Ollama
│   └── CognitiveEngine()                         # 6-phase cognition init
├── ReminderManager(speak_fn, interval=30)        # start background checker
├── MemoryStore()                                 # SQLite connection
├── UserPreferences(store)                        # preference tracker
├── HabitTracker(store)                           # habit tracker
├── get_app_index()                               # background thread: scan registry
├── _start_hotkey_listener()                      # background thread: Ctrl+Shift+Esc
├── brain.warm_up()                               # background thread: Ollama preload
├── startup_greeting(config, reminder_mgr)        # weather + rain + battery + greeting
│
└── while True:  ─────────────────────── MAIN LOOP ───────────────────────
    │
    ├── STATE: IDLE
    │   └── listen_for_wake_word()                # blocks until wake word
    │       ├── _listen_vad_short(2s)             # Silero VAD short clip
    │       ├── _get_whisper_model()              # lazy-load Whisper
    │       └── model.transcribe(wav)             # GPU Whisper STT
    │
    ├── STATE: ACTIVE
    │   ├── Check auto-sleep (90s / 180s after agent)
    │   ├── Check pending reminder announcements
    │   │
    │   ├── listen()                              # full STT
    │   │   ├── _listen_with_vad()                # Silero VAD recording
    │   │   ├── model.transcribe()                # Whisper STT
    │   │   ├── language detection                # en/hi/ne whitelist
    │   │   ├── echo detection                    # compare to last TTS
    │   │   └── noise filter (_is_noise)          # <2 chars, filler, punct
    │   │
    │   ├── _detect_meta_command(text)            # skip/shorter/repeat/undo
    │   │   └── brain.undo_last_action()          # if "undo"
    │   │
    │   ├── _is_exit_command(text)                # quit/exit/bye → farewell
    │   ├── _is_connection_command(text)           # disconnect/connect
    │   ├── _check_provider_switch(text)           # "switch to openai"
    │   ├── _is_self_test_request(text)            # "run diagnostics"
    │   │
    │   ├── if Brain available:
    │   │   ├── Timer(2.0, "Working on it...")    # instant ack
    │   │   ├── brain.think(user_input)           # ← MAIN BRAIN CALL
    │   │   │   (see Brain Flow below)
    │   │   └── cancel timer
    │   │
    │   ├── else (Brain unavailable):
    │   │   ├── detect_intent(text)               # keyword fallback
    │   │   └── action_map[intent](entity)        # execute action
    │   │
    │   └── _say(ainame, response)                # TTS output
    │       ├── _truncate_for_speech(text)         # limit to 300 chars
    │       └── speak_interruptible(text)          # Piper TTS + barge-in
    │
    └── exception handling → log + continue
```

### 2.3 Brain Flow (`brain.py::Brain.think()`)

```
Brain.think(user_input, detected_language)
│
├── Emergency stop check
├── Rate limit check
├── Auto-reset context if idle >120s
│
├── _update_topic(user_input)                # topic tracking
│   └── _cognition.run_self_analysis()       # periodic Phase 6
│
├── Phase 2: _cognition.resolve_input()      # pronoun resolution
├── One-shot language override detection      # "say X in Hindi"
├── Phase 3: _cognition.needs_decomposition() # multi-step check
│   └── _cognition.decompose()               # break into sub-goals
│
├── "do that again" / "repeat" handling       # replay _recent_actions
├── "what have you learned" handling          # _cognition.get_report()
│
├── Set execute_tool._last_user_input
├── Set execute_tool._brain_quick_chat
│
├── _classify_mode(user_input)               # quick / agent / research
│   ├── _QUICK_PATTERNS regex scan           # fast path
│   ├── _AGENT_PATTERNS regex scan           # multi-step detection
│   ├── _DIRECT_TOOL_PATTERNS               # unambiguous tool match
│   └── quick_chat() LLM classification     # fallback for ambiguous
│
├── MODE: quick
│   ├── _think_native()                      # native tool calling
│   │   ├── _call_llm_native()              # LLM with tools
│   │   │   └── _call_openai_style(tools=True) # POST to Ollama
│   │   ├── Parse tool_calls from response
│   │   ├── Validate via _resolve_tool_name()
│   │   ├── execute_tool() for each         # see Tool Execution Flow
│   │   ├── Feed results back to LLM
│   │   └── Loop up to MAX_TOOL_ROUNDS=3
│   │
│   └── _think_prompt_based()               # fallback for non-native
│       ├── _call_llm_simple()              # LLM without tools
│       ├── _parse_prompt_actions(text)     # extract JSON actions
│       └── execute_tool() for each
│
├── MODE: agent
│   └── _run_agent_mode(user_input)
│       ├── DesktopAgent(action_registry)
│       ├── agent.execute(user_input) in thread
│       ├── Monitor mic for stop words (barge-in)
│       └── Return result or interruption
│
├── MODE: research
│   └── _run_research(user_input)
│       ├── deep_research(query, llm_fn)    # multi-source web
│       └── quick_chat(synthesize_prompt)   # LLM synthesis
│
├── _collapse_completed_turn(response)      # condense tool messages
├── _sanitize_response(text)                # strip LLM artifacts
└── Return spoken response string
```

### 2.4 Tool Execution Flow (`brain.py::execute_tool()`)

```
execute_tool(tool_name, arguments, action_registry, reminder_mgr, speak_fn)
│
├── _validate_tool_choice(tool_name, user_input)  # catch LLM stickiness
│
├── Confirm sensitive tools (_CONFIRM_TOOLS)
│   └── _confirm_with_user() via speech.listen()
│
├── Phase 4: _cognition.get_confidence()          # confidence check
│   └── _cognition.find_alternative()             # suggest better tool
│
├── Response cache check (weather/time/news)       # _CACHE_TTL lookup
│   └── with _state_lock: read _response_cache
│
├── _execute_tool_inner(tool_name, arguments, ...) # ← DISPATCH
│   │
│   ├── open_app → app_finder.launch_app()
│   ├── close_app → actions.close_window()
│   ├── minimize_app → actions.minimize_window()
│   ├── google_search → actions.google_search()
│   ├── get_weather → weather.get_current_weather()
│   ├── get_forecast → weather.get_forecast()
│   ├── get_time → datetime.now().strftime()
│   ├── get_news → news.get_briefing()
│   ├── set_reminder → reminder_mgr.add_reminder()
│   ├── list_reminders → reminder_mgr.list_active()
│   ├── system_command → actions.shutdown/restart/sleep()
│   ├── toggle_setting → _toggle_system_setting()     [brain_defs]
│   ├── play_music → _play_music()                     [brain.py]
│   ├── send_email → email_sender.send_email()
│   ├── web_read → web_agent.web_read()
│   ├── web_search_answer → web_agent.web_search_extract()
│   ├── run_self_test → self_test.run_self_test()
│   ├── create_file → _execute_create_file()           [brain_defs]
│   ├── search_in_app → computer.search_in_app()
│   ├── type_text → computer.type_text()
│   ├── press_key → computer.press_key()
│   ├── click_at → computer.click_at()
│   ├── take_screenshot → vision.analyze_screen()
│   ├── find_on_screen → vision.find_element()
│   ├── run_terminal → _run_terminal()                 [brain_defs]
│   ├── manage_files → _manage_files()                 [brain_defs]
│   ├── manage_software → _manage_software()           [brain_defs]
│   ├── agent_task → DesktopAgent.execute()
│   └── dynamic tools → execute_dynamic_tool()
│
├── Store in response cache (if cacheable)
│   └── with _state_lock: write _response_cache
│
├── Record in _recent_actions
│   └── with _state_lock: append
│
├── _register_undo_for_tool()                     # reversible actions
│   └── with _state_lock: push to _undo_stack
│
├── _log_learning()                               # cognitive Phase 1
│
├── Failure recovery
│   └── app_finder.find_similar_apps()            # suggest alternatives
│
├── _verify_tool_completion()                     # post-verification
│   ├── Process check (tasklist)
│   └── Window title check (pygetwindow)
│
└── _auto_escalate_to_agent()                     # partial → agent retry
    └── _run_agent_with_timeout(goal)
```

### 2.5 Desktop Agent Flow (`desktop_agent.py::DesktopAgent.execute()`)

```
execute(goal)
│
├── _load_state(goal)                    # resume from checkpoint if exists
├── _phase_recon(goal)                   # optional reconnaissance
│   └── LLM analysis of screen state
│
├── _plan(goal)                          # LLM generates 1-10 steps
│   └── POST to Ollama /v1/chat/completions
│
├── _phase_execute(goal)
│   └── _agentic_loop(goal)              # main OBSERVE → THINK → ACT loop
│       │
│       ├── for each step (max 15 turns):
│       │   ├── _observe(goal)
│       │   │   ├── capture_screenshot()           [vision]
│       │   │   ├── Smart vision skip              # skip llava for non-visual tools
│       │   │   ├── analyze_screen()               [vision → llava]
│       │   │   ├── get_active_window_title()      [vision]
│       │   │   ├── _get_browser_url()             # PowerShell clipboard trick
│       │   │   ├── _extract_browser_content()     # web_read if URL detected
│       │   │   ├── _get_window_inventory()        [pygetwindow]
│       │   │   └── _get_running_apps()            [tasklist]
│       │   │
│       │   ├── _think(goal, screen_state)
│       │   │   ├── Build prompt with screen + plan + history
│       │   │   └── LLM decides: tool + args + reasoning
│       │   │
│       │   ├── _safety_check(decision)            # block dangerous commands
│       │   ├── _pre_action_hook(tool, args)       # prepare for action
│       │   │
│       │   ├── _act(decision)
│       │   │   ├── execute_tool(tool, args)       [brain.py]
│       │   │   └── _run_terminal_command(cmd)     # direct subprocess
│       │   │
│       │   ├── time.sleep(AFTER_ACTION_WAIT)      # 1.0s settle time
│       │   ├── _post_action_hook(tool, args, result)
│       │   │
│       │   ├── _verify_step(step, result, screen)
│       │   │   └── LLM assessment of completion
│       │   │
│       │   ├── _check_goal_done(goal)             # overall completion check
│       │   │
│       │   ├── if failed:
│       │   │   ├── _diagnose(step, error, screen) # multi-round LLM fix
│       │   │   ├── Try alternative tool (_TOOL_ALTERNATIVES)
│       │   │   └── _backtrack(goal, screen)       # undo and retry
│       │   │
│       │   └── _checkpoint(goal, screen)          # save state every 3 steps
│       │
│       └── _check_takeover(screen)                # detect login/admin prompts
│           └── _wait_for_user_takeover()           # pause for human
│
└── _phase_verify(goal, result)          # final verification
    └── LLM assessment of overall success
```

### 2.6 Speech Flow (`speech.py`)

```
LISTENING:
listen()
├── _listen_voice()                           # if voice or hybrid mode
│   ├── _listen_whisper()                     # primary STT
│   │   ├── _listen_with_vad()               # Silero VAD recording
│   │   │   ├── _get_vad_model()             # lazy-load Silero (with _vad_lock)
│   │   │   ├── PyAudio stream.read()        # 512-sample chunks at 16kHz
│   │   │   ├── VAD inference per chunk       # speech probability threshold 0.4
│   │   │   ├── Silence timeout (600ms)       # end of utterance detection
│   │   │   ├── Max speech (10s)              # prevent runaway recording
│   │   │   └── Write to temp .wav file
│   │   │
│   │   ├── _get_whisper_model()             # lazy-load (with _whisper_lock)
│   │   │   └── WhisperModel("base", device="cuda"/"cpu", compute="float16")
│   │   │
│   │   ├── model.transcribe(wav, beam_size=1, language=None)
│   │   ├── Language detection               # en/hi/ne whitelist
│   │   ├── Echo detection                   # compare to _last_spoken_text
│   │   └── Noise filter                     # _is_noise() check
│   │
│   └── _listen_google()                     # fallback STT
│       └── recognizer.listen(mic) → recognize_google()
│
└── _listen_text()                           # if text or hybrid fallback
    └── input() or sys.stdin.readline()

SPEAKING:
speak_interruptible(text)
├── Split text into sentences
├── For each sentence:
│   ├── Thread: speak(sentence)
│   │   ├── _speak_piper(text)              # English TTS (primary)
│   │   │   ├── _get_piper_voice()          # lazy-load (with _piper_lock)
│   │   │   ├── voice.synthesize(text)      # generate WAV bytes
│   │   │   └── _play_wav_data(wav_bytes)   # pygame or PowerShell playback
│   │   │
│   │   ├── _speak_gtts(text, lang)         # Hindi/Nepali/other TTS
│   │   │   ├── gTTS(text, lang=lang)       # generate MP3
│   │   │   └── pygame.mixer.music.play()   # or PowerShell fallback
│   │   │
│   │   └── _speak_pyttsx3(text)            # last-resort fallback
│   │
│   ├── Monitor mic for barge-in (0.5s intervals)
│   │   └── _listen_vad_short(2s) → speech detected?
│   └── If interrupted: stop_speaking() → return barge-in text
│
└── _is_speaking.clear()                    # echo suppression off

WAKE WORD:
listen_for_wake_word()
├── Loop:
│   ├── _listen_vad_short(max_speech_s=2, wait_timeout_s=5)
│   ├── _get_whisper_model()
│   ├── model.transcribe(wav)
│   └── Fuzzy match against _wake_words (SequenceMatcher, threshold=0.6)
└── Return on match
```

### 2.7 Provider Flow (`ai_providers.py`)

```
create_provider(name, key, prompt, model)
├── "ollama" → OllamaProvider(key, prompt, model)
├── "openai" → OpenAIProvider(key, prompt)
├── "anthropic" → AnthropicProvider(key, prompt)
└── "openrouter" → OpenRouterProvider(key, prompt)

ChatProvider.chat(user_input)
├── is_rate_limited(provider_name)?        # check _rate_limits dict
│   └── YES → _offline_fallback()          # cached response or generic
├── Append user message to context
├── _call_api()                            # provider-specific HTTP POST
│   ├── OllamaProvider → localhost:11434/v1/chat/completions
│   ├── OpenAIProvider → api.openai.com/v1/chat/completions
│   ├── AnthropicProvider → api.anthropic.com/v1/messages
│   └── OpenRouterProvider → openrouter.ai/api/v1/chat/completions
├── On success: _clear_rate_limit()
├── On 429: _record_rate_limit() (exponential backoff: 10→20→40→60s)
├── On error: _offline_fallback()
├── Append assistant response to context
├── _trim_context() (keep last 20 messages)
└── Return response text
```

### 2.8 Reminder Flow (`reminders.py`)

```
ReminderManager.__init__()
├── _load() → reminders.json
└── start_checker() → daemon thread

_check_loop() (every 30 seconds):
├── check_due() → list of due reminders
├── For each due reminder:
│   ├── speak_fn(reminder.message)
│   ├── If action_type == "execute":
│   │   └── brain.execute_tool(action_command, action_args)
│   └── fire_reminder(reminder)
│       ├── If recurring: reschedule (daily/weekly/weekdays)
│       └── Else: deactivate
└── _save() → reminders.json

add_reminder(message, time_str):
├── parse_time(time_str)           # NLP: "5pm", "in 30 min", "every Mon at 9am"
│   ├── Relative: "in X minutes/hours"
│   ├── Named: "tomorrow", "next Monday"
│   ├── Clock: "5pm", "17:00", "noon"
│   └── Recurring: "every day", "weekdays"
├── Create Reminder(id=uuid, message, trigger_time, recurrence)
└── _save()
```

---

## 3. Current Known Bugs

| # | Bug | Severity | Module | Notes |
|---|-----|----------|--------|-------|
| 0 | **`_rate_limits` undefined in `ChatProvider.chat()`** | **CRITICAL** | ai_providers.py:79 | Uses `_rate_limits.get(...)` but module uses `_provider_state`. Will crash with `NameError` on any 429. Fix: `_provider_state.rate_limits.get(...)` |
| 1 | `_play_music()` uses `getattr(execute_tool, ...)` for state | Medium | brain.py:239 | Should pass as explicit params |
| 2 | `_app_index` global mutation has no thread safety | Low | app_finder.py:349 | Initialized once in background thread |
| 3 | `_action_log` in brain.py has no lock protection | Low | brain.py:166 | GIL protects append, but not guaranteed |
| 4 | `_dynamic_tools` mutations have no lock | Low | brain.py:76 | create_tool called rarely |
| 5 | Agent `_escalation_depth` can leak on exception | Low | brain.py:1055 | Reset in finally would be safer |
| 6 | `speak_interruptible` barge-in can miss short words | Medium | speech.py:1194 | 0.5s polling interval |
| 7 | `responses.json` offline fallback returns stale answers | Low | ai_providers.py:291 | Should say "I'm offline" instead |
| 8 | OpenAI API key has 0 credits | Info | config | Using Ollama only |
| 9 | `pygame` can't build on Python 3.14 | Info | speech.py | gTTS uses PowerShell fallback |
| 10 | `smart_tester.py` vision/deep_research modes are stubs | Low | smart_tester.py | Never completes |

---

## 4. Slow Areas (Performance Bottlenecks)

| # | Area | Typical Latency | Cause | Impact |
|---|------|----------------|-------|--------|
| 1 | **Ollama cold call** | 3-8s | Model loading into VRAM | First request after idle is slow |
| 2 | **Whisper cold load** | 1-3s | Loading CUDA model | First STT after startup |
| 3 | **Piper cold load** | 0.5-1s | Loading ONNX voice model | First TTS after startup |
| 4 | **App index build** | 1-3s | Registry scan + Start Menu walk | Background, but first launch_app waits |
| 5 | **Desktop agent llava** | 2-4s per call | Vision model inference | 10 steps × 2s = 20s overhead |
| 6 | **Spotify search + play** | 5-8s | Open app + search bar + type + wait + click | Multiple time.sleep() calls |
| 7 | **`_toggle_system_setting` Bluetooth** | 3-5s | Massive PowerShell WinRT script | Could just open Settings instead |
| 8 | **deep_research** | 10-30s | Multiple web fetches + LLM synthesis | Expected for research mode |
| 9 | **News RSS** | 2-4s (cold) | Google News HTTP + XML parse | Cached for 10 min |
| 10 | **Weather API** | 1-2s (cold) | Open-Meteo HTTP + IP geolocation | Cached for 5 min |
| 11 | **`_extract_tool_from_json`** | <1ms | 165-line regex function | Rarely invoked with native tools |
| 12 | **Context collapsing** | <1ms | String manipulation | Negligible |

### Critical Path Bottleneck: Full Roundtrip

```
Wake word detection:  ~0.5s (VAD + short Whisper)
Full STT:             ~1.0s (VAD recording + Whisper transcribe)
LLM tool selection:   ~1.5-3s (Ollama warm) / 5-8s (cold)
Tool execution:       ~0.1-5s (varies by tool)
TTS response:         ~0.3-0.5s (Piper synthesis + playback start)
─────────────────────────────────────────
Total:                ~3.5-17s depending on state
```

---

## 5. Unsafe Areas (Security / Stability Risks)

| # | Risk | Module | Severity | Description |
|---|------|--------|----------|-------------|
| 1 | **Command injection via `run_terminal`** | brain_defs.py:600 | High | User voice → LLM → PowerShell. Blocklist helps but can be bypassed. |
| 2 | **File operations on home directory** | brain_defs.py:637 | Medium | `_manage_files` operates relative to `~`. Blocked dirs list helps. |
| 3 | **Dynamic tool creation via `exec()`** | brain.py:68 | High | `create_tool()` compiles and executes arbitrary Python code. |
| 4 | **Desktop agent has full system access** | desktop_agent.py | Medium | Can click anywhere, type anything, run commands. Emergency stop exists. |
| 5 | **Email credentials in plaintext fallback** | email_sender.py:55 | Medium | Legacy path stores password in JSON if `cryptography` unavailable. |
| 6 | **No input sanitization on LLM output** | brain.py | Medium | LLM-generated tool arguments fed directly to system calls. |
| 7 | **`getattr(execute_tool, ...)` pattern** | brain.py:239 | Low | Function attribute mutation is fragile and hard to test. |
| 8 | **SQLite `check_same_thread=False`** | memory.py:31 | Low | Multi-threaded SQLite without proper connection pooling. |

---

## 6. Race Conditions Due to Globals

| Global Variable | Module | Writers | Readers | Protection | Risk |
|----------------|--------|---------|---------|------------|------|
| `_undo_stack` | brain.py | `_register_undo_for_tool`, `undo_last_action` | `undo_last_action` | `_state_lock` ✅ | Safe |
| `_recent_actions` | brain.py | `execute_tool` | `Brain.think` ("do that again") | `_state_lock` ✅ | Safe |
| `_response_cache` | brain.py | `execute_tool` | `execute_tool` | `_state_lock` ✅ | Safe |
| `_escalation_depth` | brain.py | `_auto_escalate_to_agent` | `_auto_escalate_to_agent` | `_state_lock` ✅ | Safe |
| `_action_log` | brain.py | `log_action` (from many modules) | `get_action_history`, dashboard | **None** ❌ | Low (GIL helps) |
| `_dynamic_tools` | brain.py | `create_tool`, `_load_custom_tools` | `execute_dynamic_tool`, `_get_dynamic_tool_names` | **None** ❌ | Low (rare writes) |
| `_last_created_file` | brain.py | `_execute_tool_inner` | `_execute_tool_inner` | **None** ❌ | Low (single-threaded path) |
| `_experience_learner` | brain.py | `Brain.__init__` | `_log_learning` | **None** ❌ | Low (set once) |
| `_emergency_stop` | assistant.py | hotkey thread | main loop, agent | **None** ❌ | Low (bool, atomic in CPython) |
| `_assistant_state` | assistant.py | main loop | main loop | N/A (single writer) | Safe |
| `_app_index` | app_finder.py | background thread | `find_best_match`, `launch_app` | **None** ❌ | Low (set once) |
| `_rate_limits` | ai_providers.py | `_record_rate_limit`, `_clear_rate_limit` | `is_rate_limited` | **None** ❌ | Low (GIL) |
| `_ollama_available` | ai_providers.py | `check_ollama_health` | `is_rate_limited` | **None** ❌ | Low (bool) |
| `_detected_language` | speech.py | `_listen_whisper` | `get_detected_language`, `speak` | `_language_lock` ✅ | Safe |
| `_is_speaking` | speech.py | `speak`, `speak_interruptible` | `_listen_with_vad` | threading.Event ✅ | Safe |
| `_cached_config` | cognitive.py | `_get_provider_config` | `_safe_llm_call` | **None** ❌ | Low (set once) |

**Summary**: 6 unprotected globals, all low-risk due to GIL or single-writer patterns. The critical shared state (`_undo_stack`, `_recent_actions`, `_response_cache`, `_escalation_depth`) is properly protected by `_state_lock`.

**However**, there are 13 race conditions beyond just globals — including 4 HIGH-severity issues in speech.py (echo detection, mic state, language) and reminders.py (list mutation from two threads). See `docs/high-risk-areas.md` for the full analysis.

---

## 7. Top Refactor Priorities

### Priority 1: Critical Path Speed
- **Ollama warm-up**: Pre-load model on startup (already done via `brain.warm_up()`, but verify it works)
- **Response caching**: Already implemented (weather 5min, time 30s, news 10min). Extend to app_finder lookups.
- **Instant tools**: `get_time`, `list_reminders`, media keys — should bypass LLM entirely for exact keyword matches.

### Priority 2: Code Quality
- **`_play_music()` refactor**: Extract `getattr(execute_tool, ...)` → explicit parameters (like `_execute_create_file` was fixed).
- **`brain.py` still 2,953 lines**: Further split candidates: `_play_music` (164 lines), `_execute_tool_inner` (325 lines), Brain class methods for LLM calling (200 lines).
- **`desktop_agent.py` is 2,794 lines**: Split into `agent_core.py` (plan/observe/think/act) + `agent_helpers.py` (YouTube ads, Spotify, browser).

### Priority 3: Reliability
- **Agent stuck detection**: `_is_stuck()` exists but could be more aggressive. Detect repeated identical screenshots.
- **Undo edge cases**: `_escalation_depth` should use try/finally to guarantee reset.
- **`_auto_escalate_to_agent` infinite loop**: Cooldown exists (60s) but depth limit (2) should be enforced more strictly.

### Priority 4: Security
- **`run_terminal` blocklist**: Extend to cover PowerShell download cradles (`IEX`, `Invoke-Expression`).
- **`create_tool` sandboxing**: Currently uses `exec()` with full `__builtins__`. Should restrict namespace.
- **LLM output sanitization**: Validate tool arguments match expected types before execution.

### Priority 5: Testing
- **No unit tests exist**. `self_test.py` only does runtime integration checks.
- **`smart_tester.py`** is the closest thing to regression testing but requires a running Ollama instance.
- **Tool handlers are untestable**: Deeply nested in `_execute_tool_inner` with no dependency injection.
