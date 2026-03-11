# Top 20 Daily Commands

These are the most common user commands that G handles. They form the "fast path" —
any refactoring must ensure these stay fast and reliable.

| # | Command | Tool | Mode | Expected Latency |
|---|---------|------|------|-----------------|
| 1 | "open Chrome" | `open_app` | quick | <1s (app launch) |
| 2 | "open Spotify" | `open_app` | quick | <2s (Spotify slow start) |
| 3 | "what's the weather" | `get_weather` | quick | <1s (cached) / 2s (API) |
| 4 | "what time is it" | `get_time` | quick | <0.1s (local) |
| 5 | "search Google for X" | `google_search` | quick | <1s (browser open) |
| 6 | "remind me at 6pm to X" | `set_reminder` | quick | <0.5s (local) |
| 7 | "list reminders" | `list_reminders` | quick | <0.1s (local) |
| 8 | "play music" / "play X on Spotify" | `play_music` | quick | 3-8s (Spotify search + autoplay) |
| 9 | "pause music" | `play_music` | quick | <0.1s (media key) |
| 10 | "next song" / "skip" | `play_music` | quick | <0.1s (media key) |
| 11 | "close Chrome" | `close_app` | quick | <0.5s |
| 12 | "what's in the news" | `get_news` | quick | <1s (cached) / 3s (RSS) |
| 13 | "turn on dark mode" | `toggle_setting` | quick | <1s (registry) |
| 14 | "turn off Bluetooth" | `toggle_setting` | quick | 1-3s (PowerShell) |
| 15 | "create a file called X" | `create_file` | quick | 1-4s (LLM generates content) |
| 16 | "send email to X" | `send_email` | quick | 2-5s (SMTP) |
| 17 | "how much disk space" | `run_terminal` | quick | <1s (PowerShell) |
| 18 | "install VLC" | `manage_software` | quick | 30-120s (winget download) |
| 19 | "what is X" / "who is X" | `web_search_answer` | quick | 2-5s (DuckDuckGo + Wikipedia) |
| 20 | "open Settings" | `open_app` | quick | <1s (protocol URI) |

## Fast Path Requirements

All top-20 commands should route through **quick mode** (no agent, no research).

Critical latency targets:
- **Wake word → listening**: <0.5s
- **STT transcription**: <1s (GPU Whisper)
- **LLM tool selection**: <2s (Ollama warm)
- **Tool execution**: varies (see table)
- **TTS start**: <0.5s (Piper)
- **Full roundtrip** (wake → spoken response): <5s for simple commands

## Command Categories

### Instant (no network, <0.5s tool execution)
- get_time, list_reminders, pause/next/previous music, close_app, minimize_app

### Fast (local only, <2s)
- open_app, set_reminder, toggle_setting, google_search, run_terminal

### Network-dependent (1-5s)
- get_weather, get_news, web_search_answer, send_email

### Long-running (>5s)
- play_music (Spotify search), create_file (LLM generation), manage_software (winget)

---

## Optimization Opportunities

### 1. Bypass LLM for exact-match commands

Commands like "what time is it", "pause music", "next song", "list reminders" have zero ambiguity. They could be handled entirely by regex in `classify_mode()` + direct tool dispatch, skipping the 6.5s Ollama tool-calling overhead.

**Estimated savings**: 6.5s per call for ~30% of daily commands (items 4, 7, 9, 10 above).

### 2. Reduce tool schema size for Ollama

The 18-tool schema causes 6.5s LLM inference vs 0.4s without tools. Options:
- **Dynamic tool filtering**: Only send relevant tools based on `classify_mode()` pre-analysis (e.g., music commands only see `play_music`)
- **Tool grouping**: Merge rarely-used tools (e.g., `take_screenshot` + `find_on_screen` -> single `vision` tool)
- **Two-pass**: First pass with no tools to get intent, second pass with 1-3 relevant tools

### 3. Pre-warm Piper TTS on startup

Cold Piper load is 5.8s. Add `_get_piper_voice()` call to `brain.warm_up()` background thread.

### 4. Pre-warm Whisper on startup

Cold Whisper load is 1.5s. Already partially addressed by `brain.warm_up()` but should also warm Whisper model since it's needed for wake word detection.

### 5. Cache app_finder results

App fuzzy match is 0ms (fast) but the index build is 535ms. Already runs in background thread. Could persist to disk (`app_cache.json`) to avoid rebuild on restart.
