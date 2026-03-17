"""
Python wrapper for the Rust audio-capture binary.

The Rust binary handles:
  - Audio capture via cpal (Windows WASAPI / Linux ALSA / macOS CoreAudio)
  - Energy-based VAD with 30ms frames (vs ~50ms in Python)
  - Pre-buffer (90ms) + hangover (300ms) for natural speech boundaries
  - Base64-encoded f32-LE PCM utterances streamed over stdout

This wrapper:
  - Spawns and manages the binary subprocess
  - Parses newline-delimited JSON from stdout
  - Exposes thread-safe queues for VAD frames and complete utterances
  - Falls back silently when the binary is not present

Build the binary::
    python crates/build.py

Example::
    from audio.rust_audio import RustAudioCapture, is_rust_audio_available

    if is_rust_audio_available():
        cap = RustAudioCapture(vad_threshold=0.02)
        cap.start()
        utt = cap.get_utterance(timeout=5.0)  # blocks up to 5s
        if utt is not None:
            # utt.samples is np.ndarray float32 16kHz mono
            transcribe(utt.samples)
        cap.stop()
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Binary path ───────────────────────────────────────────────────────────────

_BINARY_NAME = "audio_capture.exe" if sys.platform == "win32" else "audio_capture"
BINARY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bin", _BINARY_NAME
)


class RustAudioUnavailable(RuntimeError):
    """Raised when the Rust audio binary is not compiled/installed."""


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class AudioFrame:
    """Lightweight per-frame VAD event (emitted every 30ms)."""
    timestamp: float
    is_speech: bool
    rms: float


@dataclass
class Utterance:
    """A complete speech utterance detected by the Rust VAD."""
    ts_start: float
    ts_end: float
    duration_ms: int
    # numpy float32 array — may be empty if numpy unavailable
    samples: object = field(default_factory=lambda: None)
    rms_peak: float = 0.0

    def to_wav_bytes(self, sample_rate: int = 16_000) -> bytes:
        """Convert raw samples to WAV bytes for feeding into Whisper."""
        try:
            import numpy as np
            import wave
            import io
            if self.samples is None or len(self.samples) == 0:
                return b""
            pcm_int16 = (self.samples * 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_int16.tobytes())
            return buf.getvalue()
        except Exception as e:
            logger.debug("to_wav_bytes failed: %s", e)
            return b""


# ── Capture class ─────────────────────────────────────────────────────────────

class RustAudioCapture:
    """Manages the Rust audio-capture subprocess."""

    def __init__(
        self,
        vad_threshold: float = 0.02,
        frame_ms: int = 30,
        binary_path: str = BINARY_PATH,
    ) -> None:
        self._binary_path = binary_path
        self._vad_threshold = vad_threshold
        self._frame_ms = frame_ms

        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None

        # Thread-safe output queues
        self._frame_queue: queue.Queue[AudioFrame] = queue.Queue(maxsize=200)
        self._utterance_queue: queue.Queue[Utterance] = queue.Queue(maxsize=20)

        # Confirmed config received in the "ready" message
        self._confirmed_sample_rate: int = 16_000
        self._confirmed_device: str = ""
        self._ready_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """True if the compiled binary exists at the expected path."""
        return os.path.isfile(self._binary_path)

    def start(self, timeout: float = 5.0) -> None:
        """Launch the binary and wait for it to signal readiness.

        Raises:
            RustAudioUnavailable: If the binary is not found.
            RuntimeError: If the binary fails to start within `timeout` seconds.
        """
        if not self.is_available():
            raise RustAudioUnavailable(
                f"Rust audio binary not found at: {self._binary_path}\n"
                f"Build it with:  python crates/build.py"
            )

        self._ready_event.clear()
        self._proc = subprocess.Popen(
            [self._binary_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._running = True

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="rust-audio-reader"
        )
        self._reader_thread.start()

        # Set initial VAD threshold before starting capture
        if abs(self._vad_threshold - 0.02) > 1e-6:
            self._send_cmd({"cmd": "set_vad_threshold", "value": self._vad_threshold})

        # Wait for binary to signal it is ready
        if not self._ready_event.wait(timeout=timeout):
            self.stop()
            raise RuntimeError(
                "Rust audio binary did not signal readiness within "
                f"{timeout:.0f}s — check stderr"
            )

        self._send_cmd({"cmd": "start_capture"})
        logger.info(
            "Rust audio started — device=%r  sample_rate=%d  threshold=%.3f",
            self._confirmed_device,
            self._confirmed_sample_rate,
            self._vad_threshold,
        )

    def stop(self) -> None:
        """Stop capture and terminate the binary."""
        self._running = False
        if self._proc is not None:
            try:
                self._send_cmd({"cmd": "quit"})
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        logger.debug("Rust audio stopped")

    def set_vad_threshold(self, value: float) -> None:
        """Adjust VAD sensitivity at runtime (0.001 – 1.0)."""
        self._vad_threshold = max(0.001, min(1.0, value))
        self._send_cmd({"cmd": "set_vad_threshold", "value": self._vad_threshold})

    def get_status(self) -> None:
        """Request a status report (emitted to frame_queue as-is; useful for debug)."""
        self._send_cmd({"cmd": "get_status"})

    def get_frame(self, timeout: float = 0.05) -> Optional[AudioFrame]:
        """Non-blocking poll for the next VAD frame (30ms cadence).

        Returns None if no frame is available within `timeout` seconds.
        """
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_utterance(self, timeout: float = 1.0) -> Optional[Utterance]:
        """Wait for the next complete utterance.

        Returns None if no utterance arrives within `timeout` seconds.
        """
        try:
            return self._utterance_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def device_name(self) -> str:
        return self._confirmed_device

    @property
    def sample_rate(self) -> int:
        return self._confirmed_sample_rate

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _send_cmd(self, cmd: dict) -> None:
        if self._proc is not None and self._proc.stdin:
            try:
                line = (json.dumps(cmd) + "\n").encode()
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _read_loop(self) -> None:
        """Background thread: reads newline-delimited JSON from the binary."""
        while self._running and self._proc is not None:
            try:
                raw = self._proc.stdout.readline()
            except Exception:
                break
            if not raw:
                # EOF — binary exited
                self._running = False
                break
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace").strip())
                self._handle_message(msg)
            except json.JSONDecodeError:
                pass  # Ignore non-JSON output (e.g. debug prints)

    def _handle_message(self, msg: dict) -> None:
        t = msg.get("type")

        if t == "ready":
            self._confirmed_sample_rate = msg.get("sample_rate", 16_000)
            self._confirmed_device = msg.get("device", "")
            self._ready_event.set()

        elif t == "frame":
            frame = AudioFrame(
                timestamp=msg.get("ts", 0.0),
                is_speech=bool(msg.get("is_speech", False)),
                rms=float(msg.get("rms", 0.0)),
            )
            # Drop oldest frame if queue is full (never block the reader thread)
            if self._frame_queue.full():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass

        elif t == "utterance":
            samples = _decode_samples(msg.get("audio_b64", ""))
            utt = Utterance(
                ts_start=float(msg.get("ts_start", 0.0)),
                ts_end=float(msg.get("ts_end", 0.0)),
                duration_ms=int(msg.get("duration_ms", 0)),
                samples=samples,
                rms_peak=float(msg.get("rms_peak", 0.0)),
            )
            try:
                self._utterance_queue.put_nowait(utt)
            except queue.Full:
                logger.warning("Rust audio: utterance queue full — dropping utterance")

        elif t == "error":
            logger.error("Rust audio error: %s", msg.get("msg", "unknown"))

        elif t == "status":
            logger.debug(
                "Rust audio status — capturing=%s  threshold=%.3f  device=%s",
                msg.get("capturing"),
                msg.get("vad_threshold", 0),
                msg.get("device", ""),
            )


# ── Audio decoding ─────────────────────────────────────────────────────────────

def _decode_samples(audio_b64: str):
    """Decode base64 f32-LE PCM to numpy array (or raw bytes if numpy missing)."""
    if not audio_b64:
        return None
    try:
        raw = base64.b64decode(audio_b64)
        import numpy as np
        return np.frombuffer(raw, dtype=np.float32).copy()
    except ImportError:
        # numpy not available — return raw bytes; callers must handle
        return base64.b64decode(audio_b64)
    except Exception as e:
        logger.debug("Sample decode error: %s", e)
        return None


# ── Module-level helpers ───────────────────────────────────────────────────────

def is_rust_audio_available() -> bool:
    """True if the compiled Rust binary is installed and ready to use."""
    return os.path.isfile(BINARY_PATH)


def find_binary() -> Optional[str]:
    """Return BINARY_PATH if the compiled binary exists, else None."""
    return BINARY_PATH if os.path.isfile(BINARY_PATH) else None


class RustAudioPipeline:
    """High-level pipeline wrapper around RustAudioCapture.

    Provides a simpler API expected by integration tests and external callers:
        pipeline = RustAudioPipeline()
        if pipeline.is_available:
            pipeline.start(on_speech_end=callback)
            ...
            pipeline.stop()
    """

    def __init__(self) -> None:
        self._capture: Optional[RustAudioCapture] = None
        self._on_speech_end = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def is_available(self) -> bool:
        """True if the Rust binary exists and can be launched."""
        return is_rust_audio_available()

    def start(self, on_speech_end=None) -> None:
        """Start audio capture.  ``on_speech_end(utterance)`` is called per utterance."""
        if not self.is_available:
            raise RustAudioUnavailable(
                f"Rust audio binary not found at: {BINARY_PATH}\n"
                "Build it with:  python crates/build.py"
            )
        self._on_speech_end = on_speech_end
        self._capture = RustAudioCapture()
        self._capture.start()
        self._running = True
        if on_speech_end is not None:
            self._listener_thread = threading.Thread(
                target=self._listen_loop, daemon=True, name="rust-pipeline-listener"
            )
            self._listener_thread.start()

    def stop(self) -> None:
        """Stop audio capture and join the listener thread."""
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None

    def _listen_loop(self) -> None:
        """Deliver utterances to the callback until stopped."""
        while self._running and self._capture is not None:
            utt = self._capture.get_utterance(timeout=0.5)
            if utt is not None and self._on_speech_end is not None:
                try:
                    self._on_speech_end(utt)
                except Exception as e:
                    logger.debug("RustAudioPipeline callback error: %s", e)


_singleton: Optional[RustAudioCapture] = None
_singleton_lock = threading.Lock()


def get_audio_backend() -> Optional[RustAudioCapture]:
    """Return the module-level singleton, or None if the binary is absent."""
    global _singleton
    with _singleton_lock:
        if _singleton is None and is_rust_audio_available():
            _singleton = RustAudioCapture()
        return _singleton
