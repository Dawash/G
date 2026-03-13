"""
Speech subsystem — STT, TTS, wake word, barge-in, audio state.

STT: WhisperX (4x faster, batched inference) with faster-whisper fallback.
TTS: Piper (English) + gTTS (Hindi/Nepali) + pyttsx3 fallback.
VAD: Silero neural voice activity detection.
"""

import logging
import os
import re
import sys
import tempfile
import threading
import time
import wave
import pyttsx3
import speech_recognition as sr
import numpy as np

# Fix Windows console encoding for multilingual output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Initialize engines once at module level
_engine = pyttsx3.init()
_recognizer = sr.Recognizer()

# Speed optimization: lower energy threshold so it picks up speech faster
_recognizer.energy_threshold = 300  # Lower = more sensitive (default 300)
_recognizer.dynamic_energy_threshold = True
_recognizer.pause_threshold = 0.5  # Shorter pause = faster end-of-speech detection (default 0.8)
_recognizer.phrase_threshold = 0.2  # Faster phrase start detection (default 0.3)
_recognizer.non_speaking_duration = 0.3  # Less silence needed (default 0.5)

# Calibrate ambient noise once, not every listen cycle
_calibrated = False

# Input mode: "voice", "text", or "hybrid" (voice with text fallback)
_input_mode = os.environ.get("G_INPUT_MODE", "hybrid").lower()

# TTS lock for thread safety
_tts_lock = threading.Lock()

# Barge-in: set to interrupt speech mid-sentence
_stop_speaking = threading.Event()

# Suppress listening during own TTS playback (prevents echo pickup)
_is_speaking = threading.Event()

# Global audio flag — set when ANY audio output is playing (TTS, alarm, music)
# Listening code checks this to avoid picking up system audio as speech
_audio_playing = threading.Event()

# Post-TTS cooldown duration (seconds) — ignore mic input after TTS ends
# Prevents "Going to sleep. Say Hey G to wake me." from triggering wake word
_POST_TTS_COOLDOWN_S = 2.0

# Echo detection: track last spoken text to filter self-hearing
_last_spoken_text = ""
_speak_end_time = 0.0
_echo_lock = threading.Lock()

# ===================================================================
# Mic state tracking (for dashboard and state machine)
# ===================================================================

_mic_state = "IDLE"  # IDLE | LISTENING | PROCESSING | SPEAKING
_mic_state_lock = threading.Lock()


def get_mic_state():
    """Get current mic state."""
    with _mic_state_lock:
        return _mic_state


def set_mic_state(state):
    """Set mic state (IDLE, LISTENING, PROCESSING, SPEAKING)."""
    global _mic_state
    with _mic_state_lock:
        _mic_state = state


def set_audio_playing(playing=True):
    """Signal that external audio is being output (alarm, music, etc.).

    While set, listening functions skip mic processing to avoid
    picking up system audio as user speech.
    """
    if playing:
        _audio_playing.set()
    else:
        _audio_playing.clear()


def is_audio_playing():
    """Check if any audio output is active (TTS, alarm, music)."""
    return _audio_playing.is_set() or _is_speaking.is_set()


# ===================================================================
# Wake word detection
# ===================================================================

_wake_words = set()


def _build_wake_words(ai_name):
    """Build wake word set from AI name with common mishearings."""
    name = ai_name.lower().strip()
    words = set()
    # Standard prefixes
    for prefix in ("hey ", "hey, ", "okay ", "ok ", "a ", ""):
        words.add(prefix + name)
    # Common Whisper mishearings
    if name == "g":
        words.update({"hey gee", "hey ji", "hey je", "a g", "hey g.", "heg"})
    elif name == "jarvis":
        words.update({"hey jarvis", "hey travis", "hey jarves", "jarvis", "travis"})
    return words


def init_wake_words(ai_name):
    """Initialize wake words from config AI name."""
    global _wake_words
    _wake_words = _build_wake_words(ai_name)
    logging.info(f"Wake words: {_wake_words}")


def listen_for_wake_word(timeout_s=None):
    """
    Block until wake word is detected. Uses Silero VAD with short max speech.
    Returns True on wake word match, None on timeout/silence.
    """
    from difflib import SequenceMatcher

    if not _wake_words:
        return True  # No wake words configured, always active

    set_mic_state("LISTENING")
    start = time.time()

    while True:
        if timeout_s and (time.time() - start) > timeout_s:
            set_mic_state("IDLE")
            return None

        # Skip listening while system audio is playing (TTS, alarm, music)
        if _is_speaking.is_set() or _audio_playing.is_set():
            time.sleep(0.3)
            continue

        # Post-TTS cooldown: reject wake words shortly after TTS ends
        # Prevents "Say Hey G to wake me" from triggering a false wake
        with _echo_lock:
            _time_since_tts = time.time() - _speak_end_time
        if _time_since_tts < _POST_TTS_COOLDOWN_S:
            time.sleep(0.2)
            continue

        # Use VAD with short max speech (2s for wake words)
        vad = _get_vad_model()
        if vad is None:
            # No VAD — fall back to energy-based listen + Whisper/Google STT
            logging.warning("VAD unavailable for wake word detection, falling back to energy-based listen")
            try:
                with sr.Microphone() as source:
                    if not _calibrated:
                        _recognizer.adjust_for_ambient_noise(source, duration=0.3)
                    audio = _recognizer.listen(source, timeout=5, phrase_time_limit=2)
                wav_data = audio.get_wav_data()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(wav_data)
                    fallback_wav = tmp.name
                # Transcribe with Whisper if available, else Google STT
                fb_text = None
                try:
                    fb_model = _get_whisper_model()
                    if fb_model:
                        segs, _ = fb_model.transcribe(fallback_wav, beam_size=1, language=None,
                                                       initial_prompt="G, hey G, okay G",
                                                       vad_filter=False)
                        fb_text = " ".join(s.text.strip() for s in segs).strip()
                    else:
                        fb_text = _recognizer.recognize_google(audio)
                except Exception:
                    pass
                finally:
                    try:
                        os.unlink(fallback_wav)
                    except OSError:
                        pass
                if fb_text and not _is_noise(fb_text):
                    fb_lower = fb_text.lower().strip().rstrip(".,!?")
                    if fb_lower in _wake_words:
                        set_mic_state("IDLE")
                        return True
                    for wake in _wake_words:
                        ratio = SequenceMatcher(None, fb_lower, wake).ratio()
                        if ratio >= _WAKE_WORD_FUZZY_THRESHOLD:
                            logging.info(f"Wake word fuzzy match (energy fallback): '{fb_lower}' ~ '{wake}' ({ratio:.2f})")
                            set_mic_state("IDLE")
                            return True
            except (sr.WaitTimeoutError, OSError):
                pass  # No speech detected, loop back
            continue

        wav_path = _listen_vad_short(max_speech_s=2.0, wait_timeout_s=5.0)
        if wav_path is None:
            continue

        # Transcribe quickly with Whisper
        set_mic_state("PROCESSING")
        text = None
        try:
            model = _get_whisper_model()
            if model:
                segments, _info = model.transcribe(
                    wav_path, beam_size=1, language=None,
                    initial_prompt="G, hey G, okay G",  # Vocabulary boost for wake word
                    vad_filter=False,  # Silero already filtered
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                if text and _is_noise(text):
                    text = None
        except Exception as e:
            logging.warning(f"Wake word transcription error: {e}")
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

        if not text:
            set_mic_state("LISTENING")
            continue

        text_lower = text.lower().strip().rstrip(".,!?")

        # Exact match
        if text_lower in _wake_words:
            set_mic_state("IDLE")
            return True

        # Fuzzy match
        for wake in _wake_words:
            ratio = SequenceMatcher(None, text_lower, wake).ratio()
            if ratio >= 0.6:
                logging.info(f"Wake word fuzzy match: '{text_lower}' ~ '{wake}' ({ratio:.2f})")
                set_mic_state("IDLE")
                return True

        set_mic_state("LISTENING")


def _reinitialize_mic(pa):
    """Attempt to reopen the microphone stream after a device error.

    Enumerates available input devices and opens a new PyAudio stream on the
    default (or first available) input device.

    Returns the new stream, or None if no input devices are available.
    """
    import pyaudio

    try:
        # Find an available input device
        device_count = pa.get_device_count()
        input_device_index = None
        for i in range(device_count):
            try:
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    input_device_index = i
                    break
            except Exception:
                continue

        if input_device_index is None:
            logging.error("No input devices found during mic reinitialization")
            return None

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_VAD_SAMPLE_RATE,
            input=True,
            frames_per_buffer=_VAD_CHUNK_SAMPLES,
            input_device_index=input_device_index,
        )
        dev_name = pa.get_device_info_by_index(input_device_index).get("name", "unknown")
        logging.info(f"Microphone reinitialized on device {input_device_index}: {dev_name}")
        return stream
    except Exception as e:
        logging.error(f"Failed to reinitialize microphone: {e}")
        return None


_MIC_RECOVERY_MAX_RETRIES = 3


def _listen_vad_short(max_speech_s=2.0, wait_timeout_s=5.0):
    """Short VAD listen for wake word detection. Returns WAV path or None."""
    # Don't listen while system audio is playing
    if _is_speaking.is_set() or _audio_playing.is_set():
        return None

    vad = _get_vad_model()
    if vad is None:
        return None

    try:
        import torch
        import pyaudio
    except ImportError:
        return None

    pa = None
    stream = None
    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1,
                         rate=_VAD_SAMPLE_RATE, input=True,
                         frames_per_buffer=_VAD_CHUNK_SAMPLES)

        from collections import deque
        pre_buf = deque(maxlen=_VAD_PRE_SPEECH_CHUNKS)
        speech_frames = []
        is_speaking = False
        silence_chunks = 0
        silence_needed = int(_VAD_SILENCE_TIMEOUT_MS / 32)
        max_chunks = int(max_speech_s * _VAD_SAMPLE_RATE / _VAD_CHUNK_SAMPLES)
        max_wait = int(wait_timeout_s * _VAD_SAMPLE_RATE / _VAD_CHUNK_SAMPLES)
        total = wait = 0
        mic_retries = 0

        while True:
            try:
                raw = stream.read(_VAD_CHUNK_SAMPLES, exception_on_overflow=False)
            except OSError as e:
                logging.warning(f"Microphone error in wake word listen: {e} — attempting recovery")
                mic_retries += 1
                if mic_retries > _MIC_RECOVERY_MAX_RETRIES:
                    logging.error("Microphone recovery failed after %d attempts", _MIC_RECOVERY_MAX_RETRIES)
                    vad.reset_states()
                    return None
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                time.sleep(1)
                stream = _reinitialize_mic(pa)
                if stream is None:
                    logging.error("Could not recover microphone for wake word listen")
                    vad.reset_states()
                    return None
                continue
            mic_retries = 0  # Reset on successful read
            audio = _int16_to_float32(np.frombuffer(raw, dtype=np.int16))
            conf = vad(torch.from_numpy(audio), _VAD_SAMPLE_RATE).item()

            if not is_speaking:
                pre_buf.append(raw)
                if conf >= _VAD_SPEECH_THRESHOLD:
                    is_speaking = True
                    speech_frames.extend(pre_buf)
                    speech_frames.append(raw)
                else:
                    wait += 1
                    if wait >= max_wait:
                        vad.reset_states()
                        return None
            else:
                speech_frames.append(raw)
                total += 1
                if conf < _VAD_SPEECH_THRESHOLD:
                    silence_chunks += 1
                    if silence_chunks >= silence_needed:
                        break
                else:
                    silence_chunks = 0
                if total >= max_chunks:
                    break

        vad.reset_states()
        if not speech_frames:
            return None

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_VAD_SAMPLE_RATE)
            wf.writeframes(b"".join(speech_frames))
        return tmp_path

    except Exception as e:
        logging.error(f"VAD short listen error: {e}")
        try:
            vad.reset_states()
        except Exception:
            pass
        return None
    finally:
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa:
            try:
                pa.terminate()
            except Exception:
                pass


# ===================================================================
# Silero VAD (neural voice activity detection)
# ===================================================================

_vad_model = None
_vad_lock = threading.Lock()
_vad_failed = False

_VAD_SAMPLE_RATE = 16000
_VAD_CHUNK_SAMPLES = 512  # 512 samples = 32ms at 16kHz
_VAD_MAX_SPEECH_S = 10  # Max speech duration in seconds
_VAD_PRE_SPEECH_CHUNKS = 8  # ~256ms of audio before speech start (avoids clipping)

# Configurable sensitivity — overridden from config.json if present
_VAD_SPEECH_THRESHOLD = 0.4  # Confidence threshold for speech detection
_VAD_SILENCE_TIMEOUT_MS = 900  # Silence after speech to end capture
_WAKE_WORD_FUZZY_THRESHOLD = 0.6  # SequenceMatcher ratio for fuzzy wake word matching

def _load_speech_config():
    """Load tunable VAD/wake-word settings from config.json (if available)."""
    global _VAD_SPEECH_THRESHOLD, _VAD_SILENCE_TIMEOUT_MS, _WAKE_WORD_FUZZY_THRESHOLD
    try:
        import json as _json
        _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _cfg = _json.load(_f)
            _VAD_SPEECH_THRESHOLD = float(_cfg.get("vad_threshold", _VAD_SPEECH_THRESHOLD))
            _VAD_SILENCE_TIMEOUT_MS = int(_cfg.get("vad_silence_ms", _VAD_SILENCE_TIMEOUT_MS))
            _WAKE_WORD_FUZZY_THRESHOLD = float(_cfg.get("wake_word_threshold", _WAKE_WORD_FUZZY_THRESHOLD))
    except Exception:
        pass  # Use defaults

_load_speech_config()


def _get_vad_model():
    """Lazy-load the Silero VAD model."""
    global _vad_model, _vad_failed
    if _vad_model is not None:
        return _vad_model
    if _vad_failed:
        return None

    with _vad_lock:
        if _vad_model is not None:
            return _vad_model
        if _vad_failed:
            return None

        try:
            import torch
            torch.set_num_threads(1)
            from silero_vad import load_silero_vad
            _vad_model = load_silero_vad()
            logging.info("Silero VAD model loaded successfully")
            return _vad_model
        except ImportError:
            _vad_failed = True
            logging.info("silero-vad not installed, using energy-based detection")
            return None
        except Exception as e:
            _vad_failed = True
            logging.error(f"Failed to load Silero VAD: {e}")
            return None


def _int16_to_float32(audio_int16):
    """Convert int16 numpy array to float32 normalized to [-1, 1]."""
    audio_float = audio_int16.astype(np.float32)
    max_val = np.abs(audio_float).max()
    if max_val > 0:
        audio_float /= 32768.0
    return audio_float


def _listen_with_vad():
    """
    Listen via microphone using Silero VAD for precise speech boundary detection.
    Returns path to temp WAV file containing speech, or None if no speech detected.
    """
    # Don't listen while system audio is playing
    if _is_speaking.is_set() or _audio_playing.is_set():
        return None

    vad = _get_vad_model()
    if vad is None:
        return None  # Caller should fall back to sr.Recognizer.listen()

    try:
        import torch
    except ImportError:
        logging.info("torch not available for VAD, falling back")
        return None

    try:
        import pyaudio
    except ImportError:
        logging.info("PyAudio not available for VAD, falling back")
        return None

    pa = None
    stream = None
    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_VAD_SAMPLE_RATE,
            input=True,
            frames_per_buffer=_VAD_CHUNK_SAMPLES,
        )

        print("Listening...")

        # Ring buffer for pre-speech audio (avoids clipping first syllable)
        from collections import deque
        pre_speech_buffer = deque(maxlen=_VAD_PRE_SPEECH_CHUNKS)

        speech_frames = []
        is_speaking = False
        silence_chunks = 0
        silence_chunks_needed = int(_VAD_SILENCE_TIMEOUT_MS / 32)  # 32ms per chunk
        max_chunks = int(_VAD_MAX_SPEECH_S * _VAD_SAMPLE_RATE / _VAD_CHUNK_SAMPLES)
        total_chunks = 0
        # Timeout: ~5s of total silence before giving up (no speech at all)
        max_wait_chunks = int(5.0 * _VAD_SAMPLE_RATE / _VAD_CHUNK_SAMPLES)
        wait_chunks = 0
        mic_retries = 0

        while True:
            try:
                raw = stream.read(_VAD_CHUNK_SAMPLES, exception_on_overflow=False)
            except OSError as e:
                logging.warning(f"Microphone error in VAD listen: {e} — attempting recovery")
                mic_retries += 1
                if mic_retries > _MIC_RECOVERY_MAX_RETRIES:
                    logging.error("Microphone recovery failed after %d attempts", _MIC_RECOVERY_MAX_RETRIES)
                    vad.reset_states()
                    return None
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                time.sleep(1)
                stream = _reinitialize_mic(pa)
                if stream is None:
                    logging.error("Could not recover microphone for VAD listen")
                    vad.reset_states()
                    return None
                continue
            mic_retries = 0  # Reset on successful read
            audio_int16 = np.frombuffer(raw, dtype=np.int16)
            audio_float = _int16_to_float32(audio_int16)
            tensor = torch.from_numpy(audio_float)

            confidence = vad(tensor, _VAD_SAMPLE_RATE).item()

            if not is_speaking:
                pre_speech_buffer.append(raw)
                if confidence >= _VAD_SPEECH_THRESHOLD:
                    is_speaking = True
                    silence_chunks = 0
                    # Include pre-speech buffer to avoid clipping
                    speech_frames.extend(pre_speech_buffer)
                    speech_frames.append(raw)
                    logging.debug(f"VAD: speech start (conf={confidence:.2f})")
                else:
                    wait_chunks += 1
                    if wait_chunks >= max_wait_chunks:
                        # No speech detected within timeout
                        vad.reset_states()
                        return None
            else:
                speech_frames.append(raw)
                total_chunks += 1

                if confidence < _VAD_SPEECH_THRESHOLD:
                    silence_chunks += 1
                    if silence_chunks >= silence_chunks_needed:
                        logging.debug(f"VAD: speech end after {total_chunks * 32}ms")
                        break
                else:
                    silence_chunks = 0

                if total_chunks >= max_chunks:
                    logging.debug("VAD: max speech duration reached")
                    break

        vad.reset_states()

        if not speech_frames:
            return None

        # Write speech audio to temp WAV file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(_VAD_SAMPLE_RATE)
            wf.writeframes(b"".join(speech_frames))

        return tmp_path

    except Exception as e:
        logging.error(f"VAD listen error: {e}")
        if vad is not None:
            try:
                vad.reset_states()
            except Exception:
                pass
        return None
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


# ===================================================================
# Language tracking
# ===================================================================

_detected_language = "en"  # Last detected language code (ISO 639-1)
_language_lock = threading.Lock()
_next_speak_language = None  # One-shot override: speak THIS response in this lang, then revert


def get_detected_language():
    """Get the most recently detected language code (e.g. 'en', 'hi', 'ne')."""
    with _language_lock:
        return _detected_language


def set_language(lang_code):
    """Manually set the language (e.g. 'en', 'hi', 'ne')."""
    global _detected_language
    with _language_lock:
        _detected_language = lang_code


def set_next_speak_language(lang_code):
    """Set a one-shot language override for the NEXT speak() call only.
    After speaking, it reverts to _detected_language automatically.
    Use when user says 'say X in Hindi' — the command is in English
    but the OUTPUT should be in Hindi, without permanently switching."""
    global _next_speak_language
    with _language_lock:
        _next_speak_language = lang_code


# ===================================================================
# Noise filtering (Phase 9)
# ===================================================================

_RE_PUNCTUATION_ONLY = re.compile(r'^[\.\,\!\?\s\-\—]+$')
# Exact-match set instead of regex — prevents false positives like "err" or "ohm"
_FILLER_WORDS = frozenset({
    "uh", "uhh", "uhhh", "um", "umm", "ummm", "hmm", "hmmm",
    "ah", "ahh", "ahhh", "oh", "ohh", "ohhh", "huh", "huhh",
    "mhm", "mhmm", "er", "err", "erm",
})


def _is_noise(text):
    """Return True if text is noise/filler that should be discarded."""
    if not text or len(text.strip()) < 2:
        return True
    stripped = text.strip()
    if _RE_PUNCTUATION_ONLY.match(stripped):
        return True
    # Exact match against known fillers (case-insensitive)
    clean = stripped.rstrip('.!?,').lower()
    if clean in _FILLER_WORDS:
        return True
    return False


# ===================================================================
# Whisper STT — WhisperX (4x faster) with faster-whisper fallback
# ===================================================================

_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_failed = False  # Cache failure state to avoid 80K repeated warnings
_whisper_backend = None  # "whisperx" or "faster_whisper"

# Local model path (avoids slow HuggingFace cache on Windows)
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_MODEL_DIR = os.path.join(_PROJECT_DIR, "models", "whisper-base")


def _ensure_local_model():
    """
    Ensure the Whisper model is available locally.
    Downloads via HuggingFace on first run, then copies to local dir for fast loading.
    """
    if os.path.isfile(os.path.join(_LOCAL_MODEL_DIR, "model.bin")):
        return _LOCAL_MODEL_DIR

    # First run: download via HuggingFace, then copy to local
    import shutil
    hf_cache = os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub",
        "models--Systran--faster-whisper-base", "snapshots"
    )

    if os.path.isdir(hf_cache):
        snapshots = os.listdir(hf_cache)
        if snapshots:
            snap_dir = os.path.join(hf_cache, snapshots[0])
            os.makedirs(_LOCAL_MODEL_DIR, exist_ok=True)
            for f in os.listdir(snap_dir):
                src = os.path.join(snap_dir, f)
                dst = os.path.join(_LOCAL_MODEL_DIR, f)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
            logging.info(f"Copied Whisper model to {_LOCAL_MODEL_DIR}")
            return _LOCAL_MODEL_DIR

    # Download fresh (first-ever run)
    return "base"  # Let faster-whisper download it


def _get_whisper_model():
    """Lazy-load STT model. Tries WhisperX (4x faster) then faster-whisper."""
    global _whisper_model, _whisper_failed, _whisper_backend
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_failed:
        return None

    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        if _whisper_failed:
            return None

        # Detect CUDA
        device = "cpu"
        compute_type = "int8"
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
                compute_type = "float16"
        except ImportError:
            try:
                import ctranslate2
                if "cuda" in ctranslate2.get_supported_compute_types("cuda"):
                    device = "cuda"
                    compute_type = "float16"
            except Exception:
                pass

        # Try WhisperX first (4x faster with batched inference)
        try:
            import whisperx
            logging.info(f"Loading WhisperX model on {device} ({compute_type})...")
            _whisper_model = whisperx.load_model(
                "base", device=device, compute_type=compute_type,
            )
            _whisper_backend = "whisperx"
            logging.info("WhisperX model loaded (4x faster than standard Whisper)")
            return _whisper_model
        except ImportError:
            logging.info("WhisperX not installed, trying faster-whisper...")
        except Exception as e:
            logging.warning(f"WhisperX load failed ({e}), trying faster-whisper...")

        # Fallback: faster-whisper
        try:
            from faster_whisper import WhisperModel

            model_path = _ensure_local_model()
            logging.info(f"Loading faster-whisper from {model_path} on {device} ({compute_type})...")
            _whisper_model = WhisperModel(
                model_path,
                device=device,
                compute_type=compute_type,
            )
            _whisper_backend = "faster_whisper"
            logging.info("faster-whisper model loaded successfully")

            if model_path == "base" and not os.path.isfile(
                os.path.join(_LOCAL_MODEL_DIR, "model.bin")
            ):
                _ensure_local_model()

            return _whisper_model

        except ImportError:
            _whisper_failed = True
            logging.warning("Neither whisperx nor faster-whisper installed. Using Google STT fallback.")
            return None
        except Exception as e:
            _whisper_failed = True
            logging.error(f"Failed to load Whisper model: {e}")
            return None


def _listen_whisper():
    """
    Listen via microphone and transcribe with faster-whisper.
    Uses Silero VAD for speech detection when available, falls back to
    sr.Recognizer.listen() energy-based detection.
    Returns text and updates _detected_language.
    """
    global _calibrated, _detected_language

    # Don't listen while system audio is playing (TTS, alarm, music)
    if _is_speaking.is_set() or _audio_playing.is_set():
        return None

    # Post-TTS cooldown
    with _echo_lock:
        _time_since_tts = time.time() - _speak_end_time
    if _time_since_tts < _POST_TTS_COOLDOWN_S:
        return None

    model = _get_whisper_model()
    if model is None:
        return _listen_google()  # Fallback to Google STT

    tmp_path = None
    used_silero_vad = False
    try:
        # Try Silero VAD first (better speech boundaries, no stuck-on-Listening)
        tmp_path = _listen_with_vad()

        if tmp_path is not None:
            used_silero_vad = True
        elif not _vad_failed:
            # VAD is available but detected no speech — return None to retry
            return None
        else:
            # VAD unavailable — fall back to energy-based detection
            try:
                with sr.Microphone() as source:
                    if not _calibrated:
                        print("Calibrating microphone...")
                        _recognizer.adjust_for_ambient_noise(source, duration=0.3)
                        _calibrated = True

                    if not _vad_failed:
                        # VAD returned None = no speech, don't re-print "Listening..."
                        pass
                    else:
                        print("Listening...")
                    audio = _recognizer.listen(source, timeout=3, phrase_time_limit=8)

                # Save audio to temp WAV for Whisper
                wav_data = audio.get_wav_data()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(wav_data)
                    tmp_path = tmp.name
            except sr.WaitTimeoutError:
                return None
            except OSError as e:
                logging.warning(f"Microphone unavailable: {e}")
                return "__NO_MIC__"
            except AttributeError as e:
                if "PyAudio" in str(e):
                    logging.error("PyAudio not installed. Install it or use py -3.12.")
                    return "__NO_MIC__"
                raise

        # Transcribe with Whisper (WhisperX or faster-whisper)
        try:
            _DOMAIN_PROMPT = (
                "G, Spotify, YouTube, Chrome, Firefox, Notepad, Discord, Steam, "
                "HTML, CSS, JavaScript, calculator, OpenClaw, browser, terminal, "
                "PowerShell, Kathmandu, Nepal, playlist, reminder, weather, "
                "Blinding Lights, romantic, agentic, minimize, screenshot"
            )

            text = None
            detected_lang = None
            lang_prob = 0.0

            if _whisper_backend == "whisperx":
                # WhisperX: batched inference (4x faster)
                import whisperx
                audio = whisperx.load_audio(tmp_path)
                result = model.transcribe(
                    audio,
                    batch_size=8,
                    language=None,  # Auto-detect
                )
                segments = result.get("segments", [])
                detected_lang = result.get("language", "en")
                lang_prob = 0.9  # WhisperX doesn't expose probability

                text_parts = []
                for seg in segments:
                    seg_text = seg.get("text", "").strip()
                    if seg_text:
                        text_parts.append(seg_text)
                text = " ".join(text_parts).strip()

            else:
                # faster-whisper: standard transcription
                segments, info = model.transcribe(
                    tmp_path,
                    beam_size=1,
                    language=None,
                    initial_prompt=_DOMAIN_PROMPT,
                    vad_filter=not used_silero_vad,
                    vad_parameters=dict(
                        min_silence_duration_ms=300,
                        speech_pad_ms=200,
                    ) if not used_silero_vad else None,
                )

                text_parts = []
                avg_logprob_sum = 0.0
                no_speech_sum = 0.0
                seg_count = 0
                for segment in segments:
                    text_parts.append(segment.text.strip())
                    avg_logprob_sum += getattr(segment, 'avg_logprob', 0.0)
                    no_speech_sum += getattr(segment, 'no_speech_prob', 0.0)
                    seg_count += 1

                text = " ".join(text_parts).strip()
                detected_lang = info.language
                lang_prob = info.language_probability

                # Confidence filtering (faster-whisper only — has logprob data)
                if seg_count > 0:
                    avg_logprob = avg_logprob_sum / seg_count
                    avg_no_speech = no_speech_sum / seg_count
                    if avg_logprob < -1.0 or avg_no_speech > 0.6:
                        logging.info(f"Whisper low confidence: '{text}' (logprob={avg_logprob:.2f}, no_speech={avg_no_speech:.2f})")
                        text = None

            # Echo detection: if transcription is >80% similar to last spoken text, discard
            with _echo_lock:
                _echo_text = _last_spoken_text
                _echo_time = _speak_end_time
            if text and _echo_text and time.time() - _echo_time < 3.0:
                from difflib import SequenceMatcher
                similarity = SequenceMatcher(None, text.lower(), _echo_text.lower()).ratio()
                if similarity > 0.8:
                    logging.info(f"Echo detected ({similarity:.0%} similar to TTS): '{text}'")
                    text = None

            # Noise filter: reject garbled/filler transcriptions
            if text and _is_noise(text):
                logging.debug(f"Filtered noise: '{text}'")
                text = None

            if text:
                # Update detected language with confidence gating
                detected = detected_lang
                prob = lang_prob
                if detected:
                    word_count = len(text.split())
                    with _language_lock:
                        prev_lang = _detected_language

                    # Only accept languages the user actually speaks
                    # Whisper often misdetects English as Russian, Chinese, etc.
                    _SUPPORTED_LANGS = {"en", "hi", "ne"}

                    if detected not in _SUPPORTED_LANGS:
                        logging.info(f"Whisper unsupported lang: {detected} (prob: {prob:.2f}), keeping '{prev_lang}'")
                        detected = prev_lang
                    # Low-confidence detection → keep previous language
                    elif prob < 0.7:
                        logging.info(f"Whisper low-confidence lang: {detected} (prob: {prob:.2f}), keeping '{prev_lang}'")
                        detected = prev_lang
                    # Short utterances (< 3 words) from English user → stay English
                    elif word_count < 3 and prev_lang == "en" and detected != "en":
                        logging.info(f"Whisper short utterance override: {detected} → en (only {word_count} words)")
                        detected = "en"

                    with _language_lock:
                        _detected_language = detected
                    logging.info(f"Whisper language: {detected} (raw: {info.language}, prob: {prob:.2f})")

                with _language_lock:
                    _print_lang = _detected_language
                print(f"You [{_print_lang}]: {text}")
                return text
            else:
                return None

        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    except Exception as e:
        logging.error(f"Whisper listen error: {e}")
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        logging.info("Falling back to Google STT...")
        return _listen_google()


# ===================================================================
# Google STT (online fallback)
# ===================================================================

def _listen_google():
    """Listen for voice input via microphone using Google STT. Fallback engine."""
    global _calibrated
    try:
        with sr.Microphone() as source:
            if not _calibrated:
                print("Calibrating microphone...")
                _recognizer.adjust_for_ambient_noise(source, duration=0.3)
                _calibrated = True

            print("Listening...")
            audio = _recognizer.listen(source, timeout=3, phrase_time_limit=8)

            # Use detected language for Google STT if non-English
            with _language_lock:
                lang = _detected_language
            google_lang = lang if lang in ("hi", "ne") else "en"
            # Google uses locale codes like "hi-IN", "ne-NP"
            lang_map = {"hi": "hi-IN", "ne": "ne-NP", "en": "en-US"}
            google_locale = lang_map.get(google_lang, "en-US")

            text = _recognizer.recognize_google(audio, language=google_locale)
            print(f"You: {text}")
            return text
    except sr.WaitTimeoutError:
        return None
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        logging.error(f"Speech recognition service error: {e}")
        return None
    except OSError as e:
        logging.warning(f"Microphone unavailable: {e}")
        return "__NO_MIC__"
    except AttributeError as e:
        if "PyAudio" in str(e):
            logging.error("PyAudio not installed. Install it or use py -3.12.")
            return "__NO_MIC__"
        logging.error(f"Listen error: {e}")
        return None
    except Exception as e:
        if "PyAudio" in str(e):
            logging.error("PyAudio not installed. Install it or use py -3.12.")
            return "__NO_MIC__"
        logging.error(f"Listen error: {e}")
        return None


# ===================================================================
# TTS — multilingual (pyttsx3 for English, gTTS for Hindi/Nepali/other)
# ===================================================================

_pygame_initialized = False
_pygame_lock = threading.Lock()


def _init_pygame():
    """Lazy-init pygame mixer for mp3 playback."""
    global _pygame_initialized
    if _pygame_initialized:
        return True

    with _pygame_lock:
        if _pygame_initialized:
            return True
        try:
            import pygame
            pygame.mixer.init()
            _pygame_initialized = True
            return True
        except Exception as e:
            logging.error(f"pygame mixer init failed: {e}")
            return False


def _speak_pyttsx3(text):
    """Speak with pyttsx3 (English, fast, offline). Interruptible between sentences."""
    global _engine
    import re as _re
    sentences = _re.split(r'(?<=[.!?])\s+', text)
    if not sentences:
        sentences = [text]
    with _tts_lock:
        for sentence in sentences:
            if _stop_speaking.is_set():
                _stop_speaking.clear()
                break
            if not sentence.strip():
                continue
            try:
                _engine.say(sentence)
                _engine.runAndWait()
            except RuntimeError:
                try:
                    _engine = pyttsx3.init()
                    _engine.say(sentence)
                    _engine.runAndWait()
                except Exception as e:
                    logging.error(f"TTS error after re-init: {e}")
                    print(f"[TTS Error] {sentence}")
            except Exception as e:
                logging.error(f"TTS error: {e}")
                print(f"[TTS Error] {sentence}")


# ===================================================================
# Piper TTS (neural, natural-sounding English voice)
# ===================================================================

_piper_voice = None
_piper_lock = threading.Lock()
_piper_failed = False

_PIPER_MODEL_DIR = os.path.join(_PROJECT_DIR, "models", "piper")
_PIPER_MODEL_NAME = "en_US-lessac-medium"
_PIPER_ONNX_PATH = os.path.join(_PIPER_MODEL_DIR, f"{_PIPER_MODEL_NAME}.onnx")
_PIPER_JSON_PATH = os.path.join(_PIPER_MODEL_DIR, f"{_PIPER_MODEL_NAME}.onnx.json")
_PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"


def _ensure_piper_model():
    """Download Piper voice model if not present. Returns True if model is available."""
    if os.path.isfile(_PIPER_ONNX_PATH) and os.path.isfile(_PIPER_JSON_PATH):
        return True

    os.makedirs(_PIPER_MODEL_DIR, exist_ok=True)
    try:
        import urllib.request
        if not os.path.isfile(_PIPER_ONNX_PATH):
            print(f"Downloading Piper voice model ({_PIPER_MODEL_NAME})...")
            logging.info(f"Downloading {_PIPER_MODEL_NAME}.onnx ...")
            urllib.request.urlretrieve(
                f"{_PIPER_BASE_URL}/{_PIPER_MODEL_NAME}.onnx?download=true",
                _PIPER_ONNX_PATH,
            )
        if not os.path.isfile(_PIPER_JSON_PATH):
            urllib.request.urlretrieve(
                f"{_PIPER_BASE_URL}/{_PIPER_MODEL_NAME}.onnx.json",
                _PIPER_JSON_PATH,
            )
        logging.info("Piper voice model downloaded successfully")
        return True
    except Exception as e:
        logging.error(f"Failed to download Piper model: {e}")
        # Clean up partial downloads
        for p in (_PIPER_ONNX_PATH, _PIPER_JSON_PATH):
            try:
                if os.path.isfile(p):
                    os.unlink(p)
            except OSError:
                pass
        return False


def _get_piper_voice():
    """Lazy-load the Piper TTS voice."""
    global _piper_voice, _piper_failed
    if _piper_voice is not None:
        return _piper_voice
    if _piper_failed:
        return None

    with _piper_lock:
        if _piper_voice is not None:
            return _piper_voice
        if _piper_failed:
            return None

        try:
            from piper.voice import PiperVoice
        except ImportError:
            _piper_failed = True
            logging.info("piper-tts not installed, using pyttsx3 for English TTS")
            return None

        if not _ensure_piper_model():
            _piper_failed = True
            return None

        try:
            _piper_voice = PiperVoice.load(_PIPER_ONNX_PATH)
            logging.info("Piper TTS voice loaded successfully")
            return _piper_voice
        except Exception as e:
            _piper_failed = True
            logging.error(f"Failed to load Piper voice: {e}")
            return None


def _play_wav_data(wav_bytes, sample_rate):
    """Play raw PCM int16 audio bytes. Interruptible via _stop_speaking."""
    tmp_path = None
    try:
        # Write to temp WAV file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(wav_bytes)

        # Try pygame first
        _has_pygame = False
        try:
            import pygame
            _has_pygame = True
        except ImportError:
            pass

        if _has_pygame and _init_pygame():
            import pygame
            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if _stop_speaking.is_set():
                    pygame.mixer.music.stop()
                    _stop_speaking.clear()
                    break
                pygame.time.wait(50)
        else:
            # PowerShell fallback for WAV playback
            _play_wav_fallback(tmp_path)
    finally:
        if tmp_path:
            try:
                time.sleep(0.1)
                os.unlink(tmp_path)
            except OSError:
                pass


def _play_wav_fallback(wav_path):
    """Play a WAV file using PowerShell SoundPlayer (no pygame needed)."""
    try:
        import subprocess
        ps_cmd = (
            f'$p = New-Object System.Media.SoundPlayer("{wav_path}"); '
            f'$p.PlaySync()'
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        while proc.poll() is None:
            if _stop_speaking.is_set():
                proc.terminate()
                _stop_speaking.clear()
                break
            time.sleep(0.1)
    except Exception as e:
        logging.warning(f"WAV playback fallback failed: {e}")


def _speak_piper(text):
    """Speak with Piper TTS (English, offline, natural quality). Interruptible between sentences."""
    voice = _get_piper_voice()
    if voice is None:
        _speak_pyttsx3(text)
        return

    try:
        with _tts_lock:
            # Piper's synthesize() yields AudioChunk per sentence automatically
            for audio_chunk in voice.synthesize(text):
                if _stop_speaking.is_set():
                    _stop_speaking.clear()
                    break

                pcm_data = audio_chunk.audio_int16_bytes
                if pcm_data:
                    _play_wav_data(pcm_data, audio_chunk.sample_rate)

    except Exception as e:
        logging.error(f"Piper TTS error: {e}")
        _speak_pyttsx3(text)


def _play_mp3_fallback(mp3_path):
    """Play an MP3 file using Windows Media Player COM (no pygame needed)."""
    try:
        import subprocess
        # Use PowerShell to play via Windows Media Player
        ps_cmd = (
            f'$p = New-Object System.Media.SoundPlayer; '
            f'Add-Type -AssemblyName presentationCore; '
            f'$m = New-Object System.Windows.Media.MediaPlayer; '
            f'$m.Open([Uri]"{mp3_path}"); '
            f'$m.Play(); '
            f'Start-Sleep -Seconds 1; '
            f'while ($m.Position -lt $m.NaturalDuration.TimeSpan) {{ Start-Sleep -Milliseconds 100 }}; '
            f'$m.Close()'
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait with interruptibility
        while proc.poll() is None:
            if _stop_speaking.is_set():
                proc.terminate()
                _stop_speaking.clear()
                break
            time.sleep(0.1)
        return True
    except Exception as e:
        logging.warning(f"Fallback MP3 playback failed: {e}")
        return False


def _speak_gtts(text, lang):
    """Speak with gTTS (Hindi/Nepali/other, online, good quality)."""
    try:
        from gtts import gTTS
    except ImportError:
        logging.warning("gTTS not installed. Falling back to pyttsx3.")
        _speak_pyttsx3(text)
        return

    # Validate language code — gTTS only supports certain languages
    # Common Whisper misdetections: nn (Norwegian Nynorsk), ja, zh, etc.
    _GTTS_SUPPORTED = {
        "hi", "ne", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa",  # Indian langs
        "es", "fr", "de", "it", "pt", "ru", "ja", "ko", "zh-CN", "ar",  # Major world langs
        "nl", "sv", "no", "da", "fi", "pl", "tr", "cs", "el", "ro",  # European
        "en", "id", "ms", "th", "vi", "uk", "hu", "ca", "hr", "sk",
    }
    if lang not in _GTTS_SUPPORTED:
        logging.info(f"gTTS: unsupported language '{lang}', falling back to pyttsx3")
        _speak_pyttsx3(text)
        return

    # Check if pygame is available for audio playback
    _has_pygame = False
    try:
        import pygame
        _has_pygame = True
    except ImportError:
        pass

    if _has_pygame and not _init_pygame():
        _has_pygame = False

    tmp_path = None
    try:
        # Generate speech audio
        tts = gTTS(text=text, lang=lang)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tts.save(tmp.name)
            tmp_path = tmp.name

        if _has_pygame:
            # Play with pygame (preferred — interruptible)
            import pygame
            with _tts_lock:
                pygame.mixer.music.load(tmp_path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if _stop_speaking.is_set():
                        pygame.mixer.music.stop()
                        _stop_speaking.clear()
                        break
                    pygame.time.wait(50)
        else:
            # Fallback: play with Windows PowerShell MediaPlayer
            with _tts_lock:
                _play_mp3_fallback(tmp_path)

    except Exception as e:
        logging.error(f"gTTS error: {e}")
        _speak_pyttsx3(text)
    finally:
        if tmp_path:
            if _has_pygame:
                try:
                    import pygame
                    pygame.mixer.music.unload()
                except Exception:
                    pass
            try:
                time.sleep(0.2)  # Brief delay to release file handle
                os.unlink(tmp_path)
            except OSError:
                pass


def _detect_script_language(text):
    """Detect language from script/characters in the text.

    Returns language code if non-Latin script is dominant, else None.
    This catches cases where the LLM responds in Hindi/Nepali but
    _detected_language is still 'en' (because user spoke English).
    """
    if not text:
        return None
    # Count Devanagari characters (Hindi, Nepali, Marathi, Sanskrit)
    devanagari = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    # Count CJK, Arabic, Cyrillic, etc.
    arabic = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
    # Check ratio — if >20% of non-space chars are Devanagari, it's Hindi/Nepali
    non_space = sum(1 for c in text if not c.isspace())
    if non_space == 0:
        return None
    if devanagari / non_space > 0.2:
        return "hi"  # Hindi TTS for Devanagari (better pronunciation than Nepali gTTS)
    if arabic / non_space > 0.2:
        return "ar"
    return None


def speak(text):
    """
    Speak text aloud. Auto-selects TTS engine based on detected language:
    - English → Piper (natural, offline) → fallback: pyttsx3
    - Hindi/Nepali/other → gTTS (online, natural quality)

    Supports one-shot language override via set_next_speak_language().
    Also auto-detects non-Latin scripts in the output text so LLM responses
    in Hindi/Nepali are spoken correctly even when user spoke in English.
    """
    global _next_speak_language, _last_spoken_text, _speak_end_time
    set_mic_state("SPEAKING")
    _is_speaking.set()
    with _echo_lock:
        _last_spoken_text = text[:200] if text else ""  # Record for echo detection
    try:
        # One-shot override takes priority, then falls back to detected language
        with _language_lock:
            lang = _next_speak_language or _detected_language
            _next_speak_language = None  # Clear after use — one-shot only

        # Auto-detect: if response contains non-Latin script, override language
        script_lang = _detect_script_language(text)
        if script_lang and lang == "en":
            lang = script_lang
            logging.info(f"TTS: auto-detected script language '{lang}' for non-English response")

        if lang == "en":
            _speak_piper(text)
        else:
            _speak_gtts(text, lang)
    finally:
        _is_speaking.clear()
        with _echo_lock:
            _speak_end_time = time.time()  # Silence buffer: track when TTS finished
        # Post-TTS cooldown — prevents mic picking up tail-end of TTS
        # Also prevents "Say Hey G to wake me" from triggering false wake
        time.sleep(_POST_TTS_COOLDOWN_S)
        set_mic_state("IDLE")


def speak_async(text):
    """Speak text in background thread (non-blocking)."""
    threading.Thread(target=speak, args=(text,), daemon=True).start()


def stop_speaking():
    """Immediately stop any ongoing speech. Called on barge-in."""
    _stop_speaking.set()
    # Stop pyttsx3 SAPI engine if it's active (fallback TTS)
    try:
        if _engine is not None:
            _engine.stop()
    except Exception:
        pass


def speak_interruptible(text):
    """
    Speak text but stop immediately if user starts talking (barge-in).
    Returns the user's interruption text if they interrupted, None otherwise.

    Safety: opens the mic ONCE and reuses it to avoid COM/audio conflicts
    with pyttsx3 on Windows. Adds a small delay between checks to prevent
    tight-loop resource contention.
    """
    _stop_speaking.clear()
    speak_thread = threading.Thread(target=speak, args=(text,), daemon=True)
    speak_thread.start()

    # Monitor microphone in parallel for voice activity
    # Open mic ONCE outside the loop to avoid repeated open/close
    # which causes COM conflicts with pyttsx3 SAPI on Windows
    try:
        mic = sr.Microphone()
        source = mic.__enter__()
    except Exception as e:
        logging.debug(f"Barge-in mic unavailable: {e}")
        # No mic — just wait for speech to finish (with timeout to prevent hangs)
        speak_thread.join(timeout=30)
        return None

    try:
        _barge_start = time.time()
        while speak_thread.is_alive():
            # Safety timeout: don't block forever if TTS hangs (e.g., audio device busy)
            if time.time() - _barge_start > 30:
                logging.warning("Barge-in: speech thread still alive after 30s, force-stopping")
                stop_speaking()
                break
            try:
                audio = _recognizer.listen(source, timeout=0.5, phrase_time_limit=0.5)
                # Got audio — user is speaking, stop TTS
                stop_speaking()
                speak_thread.join(timeout=1)
                # Transcribe what user said
                try:
                    interrupted_text = _recognizer.recognize_google(audio, language="en-US")
                    if interrupted_text and len(interrupted_text.strip()) > 1:
                        print(f"[Barge-in] You: {interrupted_text}")
                        return interrupted_text
                except (sr.UnknownValueError, sr.RequestError):
                    pass
                return None  # Audio detected but couldn't transcribe
            except sr.WaitTimeoutError:
                continue  # No speech detected, keep playing TTS
            except OSError:
                # Audio device error — stop monitoring, let speech finish
                logging.debug("Barge-in: audio device error, disabling monitoring")
                break
            except Exception:
                # Any other error — brief pause to avoid tight loop, then retry
                import time as _time
                _time.sleep(0.1)
                continue
    finally:
        try:
            mic.__exit__(None, None, None)
        except Exception:
            pass

    # Wait for speech to finish if we broke out of the loop
    speak_thread.join(timeout=10)
    return None  # Speech finished without interruption


# ===================================================================
# Text input
# ===================================================================

def _listen_text():
    """Get input from keyboard (or stdin PIPE in text mode)."""
    try:
        import sys as _sys
        import logging as _log
        _log.getLogger(__name__).info("_listen_text: waiting for input...")

        # In text mode (subprocess), write minimal "You:" marker via os.write (unbuffered).
        # Smart_tester's read_output() looks for "You:" to know the assistant is ready.
        # MUST include newline so readline() in the reader thread returns it.
        if os.environ.get("G_INPUT_MODE", "").lower() == "text":
            try:
                import os as _os
                _os.write(1, b"You:\n")  # Unbuffered write with newline for readline()
            except OSError:
                pass
            text = _sys.stdin.readline()
            if not text:  # EOF
                return "quit"
            text = text.strip()
        else:
            text = input("You: ").strip()

        _log.getLogger(__name__).info(f"_listen_text: got '{text[:50] if text else ''}'")
        return text if text else None
    except (EOFError, KeyboardInterrupt):
        return "quit"


# ===================================================================
# Main listen() — unified entry point
# ===================================================================

# STT engine: "whisper" or "google" (set from config or env)
_stt_engine = os.environ.get("G_STT_ENGINE", "whisper").lower()


def set_stt_engine(engine):
    """Set the STT engine: 'whisper' or 'google'."""
    global _stt_engine
    _stt_engine = engine.lower()


def _listen_voice():
    """Listen for voice input using the configured STT engine."""
    if _stt_engine == "whisper":
        return _listen_whisper()
    else:
        return _listen_google()


def listen():
    """
    Get user input via voice, text, or hybrid mode.

    Modes (set via G_INPUT_MODE env var):
      - "voice": Microphone only
      - "text": Keyboard only
      - "hybrid" (default): Try voice first, fall back to text if mic unavailable
    """
    if _input_mode == "text":
        return _listen_text()

    if _input_mode == "voice":
        return _listen_voice()

    # Hybrid mode: try voice, fall back to text
    result = _listen_voice()
    if result == "__NO_MIC__":
        print("  (Microphone not found — switching to text input)")
        return _listen_text()
    return result


