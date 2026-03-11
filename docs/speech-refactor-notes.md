# Speech Refactor Notes

**Phase**: 8 — Decompose speech.py into modular speech services

## Module Structure

```
speech_new/
  __init__.py       — Public API re-exports (matches speech.py interface)
  audio_state.py    — AudioState container (replaces ~15 globals)
  stt.py            — Silero VAD + Whisper + Google STT + noise filter
  tts.py            — Piper + gTTS + pyttsx3 + audio playback
  wakeword.py       — Wake word detection + fuzzy matching
  barge_in.py       — Interruptible speech with mic monitoring

speech.py           — Original monolith (kept as active implementation)
```

## Extracted Pieces

| Piece | From (speech.py) | To | Lines |
|-------|-------------------|----|-------|
| AudioState container | 15 globals (lines 32-68, 450-474, 816-817, 1300-1307) | `audio_state.py::AudioState` | 145 |
| Silero VAD | `_get_vad_model()`, `_listen_with_vad()`, `_int16_to_float32()`, constants (lines 264-443) | `stt.py` | |
| Whisper STT | `_get_whisper_model()`, `_ensure_local_model()`, `_listen_whisper()` (lines 496-758) | `stt.py` | |
| Google STT | `_listen_google()` (lines 765-809) | `stt.py::listen_google()` | |
| Noise filter | `_is_noise()`, regex patterns (lines 477-493) | `stt.py::is_noise()` | |
| Language tracking | `_detected_language`, `_language_lock`, `get/set/next` (lines 446-473) | `audio_state.py::AudioState` | |
| Unified listen | `listen()`, `_listen_voice()`, `_listen_text()` (lines 1296-1339) | `stt.py` | |
| pyttsx3 TTS | `_speak_pyttsx3()` (lines 839-866) | `tts.py::speak_pyttsx3()` | |
| Piper TTS | `_get_piper_voice()`, `_ensure_piper_model()`, `_speak_piper()` (lines 869-1039) | `tts.py::speak_piper()` | |
| gTTS | `_speak_gtts()` (lines 1074-1147) | `tts.py::speak_gtts()` | |
| Audio playback | `_play_wav_data()`, `_play_wav_fallback()`, `_play_mp3_fallback()`, pygame init (lines 816-1016) | `tts.py` | |
| speak() | `speak()`, `speak_async()`, `stop_speaking()` (lines 1150-1191) | `tts.py` | |
| Wake word | `_build_wake_words()`, `init_wake_words()`, `listen_for_wake_word()`, `_listen_vad_short()` (lines 70-261) | `wakeword.py` | |
| Barge-in | `speak_interruptible()` (lines 1194-1260) | `barge_in.py` | |

**Total**: stt.py ~370 lines, tts.py ~330 lines, wakeword.py ~100 lines, barge_in.py ~80 lines, audio_state.py ~145 lines

## Global State Migration

| Global | speech.py | audio_state.py |
|--------|-----------|----------------|
| `_mic_state` | Module global + Lock | `AudioState._mic_state` |
| `_calibrated` | Module global | `AudioState.calibrated` |
| `_input_mode` | Module global | `AudioState._input_mode` |
| `_tts_lock` | `threading.Lock()` | `AudioState.tts_lock` |
| `_stop_speaking` | `threading.Event()` | `AudioState.stop_speaking` |
| `_is_speaking` | `threading.Event()` | `AudioState.is_speaking` |
| `_last_spoken_text` | Module global | `AudioState._last_spoken_text` |
| `_speak_end_time` | Module global | `AudioState._speak_end_time` |
| `_detected_language` | Module global + Lock | `AudioState._detected_language` |
| `_next_speak_language` | Module global | `AudioState._next_speak_language` |
| `_stt_engine` | Module global | `AudioState._stt_engine` |
| `_wake_words` | Module global | `wakeword._wake_words` (module-level, stateless) |
| `_vad_model` | Module global + Lock | `stt._vad_model` (lazy singleton) |
| `_whisper_model` | Module global + Lock | `stt._whisper_model` (lazy singleton) |
| `_piper_voice` | Module global + Lock | `tts._piper_voice` (lazy singleton) |
| `_pygame_initialized` | Module global + Lock | `tts._pygame_initialized` |
| `_engine` | `pyttsx3.init()` | `tts._engine` |
| `_recognizer` | `sr.Recognizer()` | `stt._recognizer` |

## Timing Instrumentation

AudioState tracks these timing metrics (accessible via `get_timings()`):

| Metric | When recorded | Description |
|--------|--------------|-------------|
| `stt_duration` | After whisper transcription | Total STT time (VAD + transcribe) |
| `tts_gen_time` | After first TTS chunk generated | Time to generate first audio |
| `tts_play_delay` | After first chunk starts playing | Time from generate to playback start |
| `interruption_count` | On stop_speaking() or barge-in | Total barge-in interruptions |

## Engine Fallback Chains

### STT (Speech-to-Text)
```
1. Silero VAD + faster-whisper (GPU CUDA float16, offline)
   |-- VAD unavailable? -> sr.Recognizer energy-based + faster-whisper
   |-- Whisper unavailable? -> Google STT (online)
   |-- Google fails? -> return None
```

### TTS (Text-to-Speech)
```
English:
  1. Piper neural TTS (offline, natural voice)
     |-- Piper unavailable? -> pyttsx3 SAPI (offline, robotic)

Non-English (hi, ne, etc.):
  1. gTTS (online, Google TTS)
     |-- gTTS unavailable or unsupported lang? -> pyttsx3 SAPI

Audio playback:
  1. pygame.mixer (preferred, interruptible)
     |-- pygame unavailable? -> PowerShell SoundPlayer (WAV) or MediaPlayer (MP3)
```

### Wake Word Detection
```
1. Silero VAD (2s short clips) + Whisper transcription + fuzzy match
   |-- VAD unavailable? -> always active (skip wake word)
```

## Remaining Coupling

1. **speech.py is still the active implementation** — all callers (`assistant_loop.py`, `brain.py`, `tools/safety_policy.py`, `desktop_agent.py`, `dashboard/voice_thread.py`) import from `speech`, not `speech_new`.

2. **brain.py internal imports** — `brain.py:1442` imports `_get_whisper_model`, `_listen_vad_short`, `_is_noise` directly from speech.py for the Brain's built-in recording feature. These exist in `speech_new.stt` as `get_whisper_model`, `listen_vad`, `is_noise`.

3. **self_test.py** — imports `_play_mp3_fallback` from speech.py. Available as `speech_new.tts.play_mp3_fallback`.

4. **debug/perf_baseline.py** — imports `_get_whisper_model`, `_speak_piper` from speech.py.

5. **interactive_test.py** — imports `speak` and `speech as speech_mod`.

## Migration Path

To switch a caller from speech.py to speech_new:

```python
# Before:
from speech import listen, speak, speak_interruptible, stop_speaking
from speech import init_wake_words, listen_for_wake_word
from speech import get_detected_language, set_language, set_stt_engine

# After (same API):
from speech_new import listen, speak, speak_interruptible, stop_speaking
from speech_new import init_wake_words, listen_for_wake_word
from speech_new import get_detected_language, set_language, set_stt_engine
```

Internal functions map:
```python
speech._get_whisper_model  -> speech_new.stt.get_whisper_model
speech._listen_vad_short   -> speech_new.stt.listen_vad  (renamed: max_speech_s param)
speech._is_noise           -> speech_new.stt.is_noise
speech._play_mp3_fallback  -> speech_new.tts.play_mp3_fallback
speech._speak_piper        -> speech_new.tts.speak_piper
```

## Possible Future Improvements

1. **Caller migration** — Switch orchestration/assistant_loop.py to import from speech_new, then brain.py, then delete speech.py.
2. **STT plugin system** — Abstract STT engines behind a common interface (WhisperSTT, GoogleSTT) for easier swapping.
3. **TTS plugin system** — Abstract TTS engines (PiperTTS, GttsTTS, Pyttsx3TTS) with a common `synthesize(text, lang) -> audio` interface.
4. **Audio device management** — Centralized PyAudio instance instead of creating/destroying per-listen.
5. **Streaming STT** — Stream audio to Whisper in real-time instead of record-then-transcribe.
6. **core.state.AudioState integration** — The `core/state.py::AudioState` dataclass could replace `speech_new.audio_state.AudioState` once the DI container wires them together.
