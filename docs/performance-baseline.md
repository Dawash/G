# Performance Baseline

**Measured**: 2026-03-07 00:48:43

| Measurement | Avg (ms) | Rounds | Notes |
|------------|----------|--------|-------|
| import_config | 36.3 | 1 | <module 'config' from 'C:\\dev\\G\\config.py'> |
| import_ai_providers | 77.9 | 1 | <module 'ai_providers' from 'C:\\dev\\G\\ai_providers.py'> |
| import_speech | 182.9 | 1 | <module 'speech' from 'C:\\dev\\G\\speech.py'> |
| import_intent | 0.4 | 1 | <module 'intent' from 'C:\\dev\\G\\intent.py'> |
| import_actions | 16.8 | 1 | <module 'actions' from 'C:\\dev\\G\\actions.py'> |
| import_app_finder | 0.0 | 1 | <module 'app_finder' from 'C:\\dev\\G\\app_finder.py'> |
| import_brain_defs | 9.4 | 1 | <module 'brain_defs' from 'C:\\dev\\G\\brain_defs.py'> |
| import_brain | 36.5 | 1 | <module 'brain' from 'C:\\dev\\G\\brain.py'> |
| import_desktop_agent | 36.1 | 1 | <module 'desktop_agent' from 'C:\\dev\\G\\desktop_agent.py'> |
| import_vision | 0.3 | 1 | <module 'vision' from 'C:\\dev\\G\\vision.py'> |
| import_computer | 0.4 | 1 | <module 'computer' from 'C:\\dev\\G\\computer.py'> |
| import_web_agent | 0.4 | 1 | <module 'web_agent' from 'C:\\dev\\G\\web_agent.py'> |
| import_email_sender | 15.8 | 1 | <module 'email_sender' from 'C:\\dev\\G\\email_sender.py'> |
| import_memory | 3.2 | 1 | <module 'memory' from 'C:\\dev\\G\\memory.py'> |
| import_cognitive | 0.5 | 1 | <module 'cognitive' from 'C:\\dev\\G\\cognitive.py'> |
| import_weather | 0.4 | 1 | <module 'weather' from 'C:\\dev\\G\\weather.py'> |
| import_reminders | 0.9 | 1 | <module 'reminders' from 'C:\\dev\\G\\reminders.py'> |
| import_news | 5.8 | 1 | <module 'news' from 'C:\\dev\\G\\news.py'> |
| import_self_test | 0.3 | 1 | <module 'self_test' from 'C:\\dev\\G\\self_test.py'> |
| config_load | 1.4 | 1 | {'username': 'dawa', 'ai_name': 'G', 'provider': 'ollama', 'ollama_model': 'qwen |
| ollama_health | 10.3 | 3 | 200 |
| whisper_model_cold | 1468.1 | 1 | <faster_whisper.transcribe.WhisperModel object at 0x0000015A6E7020C0> |
| whisper_model_warm | 0.0 | 1 | <faster_whisper.transcribe.WhisperModel object at 0x0000015A6E7020C0> |
| build_tool_definitions | 0.0 | 5 | [{'type': 'function', 'function': {'name': 'open_app', 'description': "Open any  |
| build_core_tools | 0.0 | 5 | [{'type': 'function', 'function': {'name': 'open_app', 'description': "Open any  |
| brain_init | 4.3 | 1 | <brain.Brain object at 0x0000015A7A6CF5C0> |
| ollama_cold_call | 3162.3 | 1 | Hi |
| ollama_warm_simple | 373.9 | 3 | Four |
| ollama_warm_with_tools | 6488.9 | 3 | I'm sorry for any confusion, but I can't directly open applications on your devi |
| tool_get_time | 0.0 | 1 | 12:48 AM, Saturday March 07 2026 |
| tool_get_weather | 1351.8 | 1 | In Frankfurt am Main, it's 59�F (15.0�C) with overcast. |
| tool_get_news | 1930.9 | 1 | Here are today's top headlines. First, Middle East war: Trump says no deal with  |
| app_index_build | 534.7 | 1 | 144 |
| app_fuzzy_match | 0.0 | 5 | {'name': 'chrome', 'exe_path': 'C:\\Program Files\\Google\\Chrome\\Application\\ |
| tts_piper_hello | 5847.4 | 1 | OK |

## Key Findings

- **Total module import time**: 424 ms
- **Slowest import**: import_speech (183 ms)
- **Ollama cold call**: 3162.3 ms
- **Ollama warm call (no tools)**: 373.9 ms
- **Ollama warm call (with 18 tools)**: 6488.9 ms (**17x slower than without tools**)
- **Brain init**: 4.3 ms
- **Weather API**: 1351.8 ms
- **News RSS**: 1930.9 ms
- **App index build**: 534.7 ms
- **App fuzzy match**: 0.0 ms
- **TTS (Piper) cold**: 5847.4 ms (includes ONNX model load)
- **Whisper cold load**: 1468.1 ms
- **Whisper warm load**: 0.0 ms

## Critical Path Analysis

The dominant bottleneck for user-perceived latency:

```
Component              Cold (ms)    Warm (ms)    Notes
───────────────────────────────────────────────────────────────
VAD recording          300-800      300-800      Fixed (waiting for speech end)
Whisper STT            1468+500     500          Cold = model load + transcribe
LLM tool selection     3162+6489    6489         18-tool schema adds ~6s overhead
Tool execution         0-5000       0-5000       Varies by tool
Piper TTS              5847         300          Cold = ONNX model load
───────────────────────────────────────────────────────────────
Total (cold)           ~17s         —            First request after startup
Total (warm)           —            ~8s          Steady-state with tools
Total (warm, no tools) —            ~2s          quick_chat() path
```

**Key insight**: Ollama with 18 tools takes 6.5s vs 0.4s without tools (16x overhead).
This means the tool schema itself is the primary latency bottleneck for qwen2.5:7b.

## Baseline Metrics Checklist

Use this checklist to measure impact of any refactoring. All times in milliseconds.

| Metric | Baseline | Target | How to Measure |
|--------|----------|--------|----------------|
| **Startup time** (run.py -> assistant ready) | ~5,000-15,000 | <5,000 | `time python run.py` until "Listening..." |
| **Wake word latency** (speech end -> wake detected) | ~500 | <500 | Timestamp in `listen_for_wake_word()` |
| **STT latency** (speech end -> text available) | 500 (warm), 1968 (cold) | <800 | Whisper transcribe() timing |
| **Mode classification** | <1 (regex), ~400 (LLM) | <1 (regex) | `classify_mode()` timing |
| **LLM tool selection** (prompt -> tool chosen) | 6,489 (with tools) | <2,000 | `_call_llm_native()` timing |
| **LLM quick_chat** (prompt -> text response) | 374 | <500 | `quick_chat()` timing |
| **Tool execution** (call -> result) | 0-5,000 | <1,000 (local tools) | `_execute_tool_inner()` timing |
| **TTS start** (text -> audio begins) | 300 (warm), 5,847 (cold) | <500 | `speak()` / `_speak_piper()` timing |
| **Full roundtrip** (wake -> spoken response) | ~8,000 (warm) | <5,000 | End-to-end in main loop |
| **Idle CPU usage** | Not measured | <2% | Task Manager / `psutil` |
| **Memory usage** (steady state) | Not measured | <500 MB | Task Manager / `psutil` |
| **App index build** | 535 | <500 | `get_app_index()` timing |

### How to run baseline measurements

```python
# Add to self_test.py or run standalone:
import time

# 1. Startup time
t0 = time.perf_counter()
import assistant
# ... init code ...
print(f"Startup: {(time.perf_counter()-t0)*1000:.0f}ms")

# 2. STT latency
t0 = time.perf_counter()
text = speech._listen_whisper()
print(f"STT: {(time.perf_counter()-t0)*1000:.0f}ms")

# 3. LLM with tools
t0 = time.perf_counter()
result = brain._call_llm_native(messages, tools)
print(f"LLM+tools: {(time.perf_counter()-t0)*1000:.0f}ms")

# 4. TTS
t0 = time.perf_counter()
speech.speak("Hello world")
print(f"TTS: {(time.perf_counter()-t0)*1000:.0f}ms")
```

### TODO: Automated benchmark script

- [ ] Create `benchmarks.py` that runs all metrics above in sequence
- [ ] Output results in same table format as this doc for easy comparison
- [ ] Run before and after each refactor phase
- [ ] Track historical results in `docs/benchmark-history.md`
