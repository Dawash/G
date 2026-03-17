"""Tests for audio/rust_audio.py — Rust audio capture pipeline.

All tests mock the binary subprocess so they pass without Rust installed.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import sys
import threading
import time
from io import BytesIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from audio.rust_audio import (
    AudioFrame,
    RustAudioCapture,
    RustAudioUnavailable,
    Utterance,
    BINARY_PATH,
    _decode_samples,
    get_audio_backend,
    is_rust_audio_available,
)


# =============================================================================
# Helpers
# =============================================================================

def _make_ready_msg(
    sample_rate: int = 16000,
    frame_ms: int = 30,
    device: str = "Test Mic",
    threshold: float = 0.02,
) -> bytes:
    return (
        json.dumps({
            "type": "ready",
            "sample_rate": sample_rate,
            "frame_ms": frame_ms,
            "channels": 1,
            "vad_threshold": threshold,
            "device": device,
        }) + "\n"
    ).encode()


def _make_frame_msg(ts: float = 1.0, is_speech: bool = False, rms: float = 0.01) -> bytes:
    return (json.dumps({"type": "frame", "ts": ts, "is_speech": is_speech, "rms": rms}) + "\n").encode()


def _make_utterance_msg(duration_ms: int = 500, rms_peak: float = 0.1) -> bytes:
    # 500ms of silence at 16kHz = 8000 samples
    n_samples = 8000
    samples = [0.05] * n_samples
    raw = b"".join(struct.pack("<f", s) for s in samples)
    audio_b64 = base64.b64encode(raw).decode()
    return (
        json.dumps({
            "type": "utterance",
            "ts_start": 1.0,
            "ts_end": 1.5,
            "duration_ms": duration_ms,
            "audio_b64": audio_b64,
            "rms_peak": rms_peak,
        }) + "\n"
    ).encode()


def _make_error_msg(msg: str = "device unavailable") -> bytes:
    return (json.dumps({"type": "error", "msg": msg}) + "\n").encode()


def _mock_proc(stdout_lines: list[bytes]) -> MagicMock:
    """Create a mock Popen object that yields the given stdout lines."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stderr = MagicMock()
    responses = list(stdout_lines) + [b""]  # EOF at end
    proc.stdout.readline.side_effect = responses
    proc.wait.return_value = 0
    return proc


# =============================================================================
# Module-level helpers
# =============================================================================

class TestIsRustAudioAvailable:
    def test_false_when_no_binary(self):
        with patch("os.path.isfile", return_value=False):
            assert is_rust_audio_available() is False

    def test_true_when_binary_exists(self):
        with patch("os.path.isfile", return_value=True):
            assert is_rust_audio_available() is True

    def test_binary_path_is_platform_specific(self):
        assert "audio_capture" in BINARY_PATH
        assert "bin" in BINARY_PATH


# =============================================================================
# AudioFrame dataclass
# =============================================================================

class TestAudioFrame:
    def test_fields(self):
        f = AudioFrame(timestamp=1.23, is_speech=True, rms=0.05)
        assert f.timestamp == 1.23
        assert f.is_speech is True
        assert f.rms == pytest.approx(0.05)

    def test_is_speech_false(self):
        f = AudioFrame(timestamp=0.0, is_speech=False, rms=0.001)
        assert f.is_speech is False

    def test_dataclass(self):
        from dataclasses import fields
        names = {fi.name for fi in fields(AudioFrame)}
        assert names == {"timestamp", "is_speech", "rms"}


# =============================================================================
# Utterance dataclass
# =============================================================================

class TestUtterance:
    def test_fields(self):
        u = Utterance(ts_start=1.0, ts_end=1.5, duration_ms=500)
        assert u.ts_start == 1.0
        assert u.duration_ms == 500
        assert u.samples is None

    def test_to_wav_bytes_no_samples(self):
        u = Utterance(ts_start=0.0, ts_end=0.0, duration_ms=0)
        assert u.to_wav_bytes() == b""

    def test_to_wav_bytes_with_numpy(self):
        pytest.importorskip("numpy")
        import numpy as np
        u = Utterance(ts_start=0.0, ts_end=0.1, duration_ms=100,
                      samples=np.zeros(1600, dtype=np.float32))
        wav = u.to_wav_bytes()
        assert wav[:4] == b"RIFF"  # WAV header

    def test_rms_peak_default(self):
        u = Utterance(ts_start=0.0, ts_end=0.0, duration_ms=0)
        assert u.rms_peak == 0.0


# =============================================================================
# RustAudioCapture.is_available
# =============================================================================

class TestIsAvailable:
    def test_false_when_no_binary(self):
        cap = RustAudioCapture(binary_path="/nonexistent/binary")
        assert cap.is_available() is False

    def test_true_when_binary_present(self, tmp_path):
        fake_bin = tmp_path / "audio_capture"
        fake_bin.write_bytes(b"fake")
        cap = RustAudioCapture(binary_path=str(fake_bin))
        assert cap.is_available() is True


# =============================================================================
# RustAudioCapture.start — binary missing
# =============================================================================

class TestStartNoBinary:
    def test_raises_when_no_binary(self):
        cap = RustAudioCapture(binary_path="/no/such/binary")
        with pytest.raises(RustAudioUnavailable):
            cap.start()

    def test_error_message_contains_path(self):
        cap = RustAudioCapture(binary_path="/no/such/binary")
        with pytest.raises(RustAudioUnavailable, match="/no/such/binary"):
            cap.start()


# =============================================================================
# RustAudioCapture.start — happy path (mocked binary)
# =============================================================================

class TestStartMocked:
    def _cap_and_proc(self, extra_lines=None):
        lines = [_make_ready_msg(device="Mock Mic")] + (extra_lines or [])
        proc = _mock_proc(lines)
        cap = RustAudioCapture(binary_path="/fake/binary")
        return cap, proc

    def test_start_launches_popen(self):
        cap, proc = self._cap_and_proc()
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
        assert cap._proc is not None

    def test_start_sends_start_capture_command(self):
        cap, proc = self._cap_and_proc()
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
        written = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
        assert b"start_capture" in written

    def test_start_populates_device_name(self):
        cap, proc = self._cap_and_proc()
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
        assert cap.device_name == "Mock Mic"

    def test_start_populates_sample_rate(self):
        cap, proc = self._cap_and_proc()
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
        assert cap.sample_rate == 16_000

    def test_start_timeout_raises(self):
        """If binary never sends 'ready', start() raises RuntimeError."""
        proc = _mock_proc([b""])  # instant EOF, no ready message
        cap = RustAudioCapture(binary_path="/fake/binary")
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            with pytest.raises(RuntimeError, match="readiness"):
                cap.start(timeout=0.1)


# =============================================================================
# RustAudioCapture.stop
# =============================================================================

class TestStop:
    def test_stop_sends_quit(self):
        proc = _mock_proc([_make_ready_msg()])
        cap = RustAudioCapture(binary_path="/fake/binary")
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
            cap.stop()
        written = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
        assert b"quit" in written

    def test_stop_calls_wait(self):
        proc = _mock_proc([_make_ready_msg()])
        cap = RustAudioCapture(binary_path="/fake/binary")
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
            cap.stop()
        proc.wait.assert_called()

    def test_stop_clears_proc(self):
        proc = _mock_proc([_make_ready_msg()])
        cap = RustAudioCapture(binary_path="/fake/binary")
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
            cap.stop()
        assert cap._proc is None

    def test_stop_when_not_started_is_safe(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        cap.stop()  # Should not raise


# =============================================================================
# RustAudioCapture.set_vad_threshold
# =============================================================================

class TestSetVadThreshold:
    def test_sends_correct_command(self):
        proc = _mock_proc([_make_ready_msg()])
        cap = RustAudioCapture(binary_path="/fake/binary")
        with patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", return_value=proc):
            cap.start(timeout=2.0)
            proc.stdin.write.reset_mock()
            cap.set_vad_threshold(0.05)
        written = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
        cmd = json.loads(written.decode().strip())
        assert cmd["cmd"] == "set_vad_threshold"
        assert abs(cmd["value"] - 0.05) < 1e-6

    def test_threshold_clamped_low(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        cap._proc = MagicMock()
        cap._proc.stdin = MagicMock()
        cap.set_vad_threshold(-1.0)
        assert cap._vad_threshold >= 0.001

    def test_threshold_clamped_high(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        cap._proc = MagicMock()
        cap._proc.stdin = MagicMock()
        cap.set_vad_threshold(999.0)
        assert cap._vad_threshold <= 1.0


# =============================================================================
# _handle_message — message parsing
# =============================================================================

class TestHandleMessage:
    def _cap(self) -> RustAudioCapture:
        return RustAudioCapture(binary_path="/fake/binary")

    def test_frame_message_enqueued(self):
        cap = self._cap()
        cap._handle_message({"type": "frame", "ts": 1.5, "is_speech": True, "rms": 0.08})
        frame = cap._frame_queue.get_nowait()
        assert frame.is_speech is True
        assert frame.rms == pytest.approx(0.08)

    def test_utterance_message_enqueued(self):
        cap = self._cap()
        msg = json.loads(_make_utterance_msg().decode().strip())
        cap._handle_message(msg)
        utt = cap._utterance_queue.get_nowait()
        assert utt.duration_ms == 500
        assert utt.ts_start == pytest.approx(1.0)

    def test_utterance_samples_decoded(self):
        pytest.importorskip("numpy")
        cap = self._cap()
        msg = json.loads(_make_utterance_msg().decode().strip())
        cap._handle_message(msg)
        utt = cap._utterance_queue.get_nowait()
        assert utt.samples is not None
        assert len(utt.samples) == 8000

    def test_error_message_does_not_raise(self):
        cap = self._cap()
        # Should log, not raise
        cap._handle_message({"type": "error", "msg": "device crash"})

    def test_ready_message_sets_event(self):
        cap = self._cap()
        cap._handle_message({
            "type": "ready", "sample_rate": 16000, "device": "Test",
            "frame_ms": 30, "channels": 1, "vad_threshold": 0.02,
        })
        assert cap._ready_event.is_set()
        assert cap.device_name == "Test"

    def test_frame_queue_overflow_drops_oldest(self):
        cap = self._cap()
        # Fill queue to max
        for i in range(cap._frame_queue.maxsize):
            cap._frame_queue.put(AudioFrame(timestamp=float(i), is_speech=False, rms=0.0))
        # This should drop oldest and enqueue new one
        cap._handle_message({"type": "frame", "ts": 999.0, "is_speech": True, "rms": 0.1})
        # Queue should still be at maxsize, last entry should be the new one
        items = []
        while not cap._frame_queue.empty():
            items.append(cap._frame_queue.get_nowait())
        # The newest item (ts=999.0) should be present
        assert any(f.timestamp == pytest.approx(999.0) for f in items)

    def test_unknown_message_type_ignored(self):
        cap = self._cap()
        cap._handle_message({"type": "heartbeat", "data": 42})  # Should not raise


# =============================================================================
# get_frame / get_utterance
# =============================================================================

class TestGetFrameAndUtterance:
    def test_get_frame_returns_none_on_timeout(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        result = cap.get_frame(timeout=0.01)
        assert result is None

    def test_get_frame_returns_queued_item(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        cap._frame_queue.put(AudioFrame(timestamp=5.0, is_speech=True, rms=0.1))
        frame = cap.get_frame(timeout=0.1)
        assert frame is not None
        assert frame.timestamp == pytest.approx(5.0)

    def test_get_utterance_returns_none_on_timeout(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        result = cap.get_utterance(timeout=0.01)
        assert result is None

    def test_get_utterance_returns_queued_item(self):
        cap = RustAudioCapture(binary_path="/fake/binary")
        utt = Utterance(ts_start=1.0, ts_end=2.0, duration_ms=1000)
        cap._utterance_queue.put(utt)
        result = cap.get_utterance(timeout=0.1)
        assert result is not None
        assert result.duration_ms == 1000


# =============================================================================
# Read loop
# =============================================================================

class TestReadLoop:
    def _run_loop(self, lines: list[bytes]) -> RustAudioCapture:
        """Run _read_loop in a thread with mock stdout lines."""
        cap = RustAudioCapture(binary_path="/fake/binary")
        cap._running = True
        responses = list(lines) + [b""]

        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = responses
        cap._proc = mock_proc

        t = threading.Thread(target=cap._read_loop, daemon=True)
        t.start()
        t.join(timeout=2.0)
        return cap

    def test_parses_frame_messages(self):
        cap = self._run_loop([_make_frame_msg(ts=7.0, is_speech=True)])
        frame = cap._frame_queue.get(timeout=1.0)
        assert frame.timestamp == pytest.approx(7.0)

    def test_parses_utterance_messages(self):
        cap = self._run_loop([_make_utterance_msg(duration_ms=800)])
        utt = cap._utterance_queue.get(timeout=1.0)
        assert utt.duration_ms == 800

    def test_handles_malformed_json(self):
        cap = self._run_loop([b"not valid json\n", _make_frame_msg(ts=3.0)])
        frame = cap._frame_queue.get(timeout=1.0)
        assert frame.timestamp == pytest.approx(3.0)

    def test_stops_on_eof(self):
        cap = self._run_loop([b""])  # immediate EOF
        assert not cap._running

    def test_multiple_messages(self):
        lines = [_make_frame_msg(ts=float(i)) for i in range(5)]
        cap = self._run_loop(lines)
        time.sleep(0.1)
        assert cap._frame_queue.qsize() == 5


# =============================================================================
# _decode_samples
# =============================================================================

class TestDecodeSamples:
    def test_empty_string_returns_none(self):
        assert _decode_samples("") is None

    def test_valid_b64_returns_array(self):
        pytest.importorskip("numpy")
        import numpy as np
        samples = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        b64 = base64.b64encode(samples.tobytes()).decode()
        result = _decode_samples(b64)
        assert result is not None
        assert len(result) == 3

    def test_invalid_b64_returns_none(self):
        result = _decode_samples("!!!not_valid_base64!!!")
        assert result is None

    def test_correct_float_values(self):
        pytest.importorskip("numpy")
        import numpy as np
        original = np.array([0.5, -0.5, 1.0, 0.0], dtype=np.float32)
        b64 = base64.b64encode(original.tobytes()).decode()
        decoded = _decode_samples(b64)
        np.testing.assert_allclose(decoded, original, rtol=1e-6)


# =============================================================================
# get_audio_backend
# =============================================================================

class TestGetAudioBackend:
    def test_returns_none_when_no_binary(self):
        import audio.rust_audio as ra
        original = ra._singleton
        try:
            ra._singleton = None
            with patch("audio.rust_audio.is_rust_audio_available", return_value=False):
                result = get_audio_backend()
            assert result is None
        finally:
            ra._singleton = original

    def test_returns_instance_when_binary_available(self):
        import audio.rust_audio as ra
        original = ra._singleton
        try:
            ra._singleton = None
            with patch("audio.rust_audio.is_rust_audio_available", return_value=True):
                result = get_audio_backend()
            assert isinstance(result, RustAudioCapture)
        finally:
            ra._singleton = original

    def test_returns_same_singleton(self):
        import audio.rust_audio as ra
        original = ra._singleton
        try:
            ra._singleton = None
            with patch("audio.rust_audio.is_rust_audio_available", return_value=True):
                a = get_audio_backend()
                b = get_audio_backend()
            assert a is b
        finally:
            ra._singleton = original

    def test_thread_safety(self):
        """Multiple threads calling get_audio_backend() get the same singleton."""
        import audio.rust_audio as ra
        original = ra._singleton
        results = []
        try:
            ra._singleton = None
            with patch("audio.rust_audio.is_rust_audio_available", return_value=True):
                def _get():
                    results.append(get_audio_backend())
                threads = [threading.Thread(target=_get) for _ in range(10)]
                [t.start() for t in threads]
                [t.join() for t in threads]
            ids = {id(r) for r in results if r is not None}
            assert len(ids) == 1, "All threads should get the same singleton"
        finally:
            ra._singleton = original


# =============================================================================
# Build script
# =============================================================================

class TestBuildScript:
    def test_build_script_exists(self):
        path = os.path.join(ROOT, "crates", "build.py")
        assert os.path.isfile(path)

    def test_check_rust_function_exists(self):
        path = os.path.join(ROOT, "crates", "build.py")
        with open(path) as f:
            src = f.read()
        assert "def check_rust" in src

    def test_build_function_exists(self):
        path = os.path.join(ROOT, "crates", "build.py")
        with open(path) as f:
            src = f.read()
        assert "def build" in src

    def test_check_rust_returns_false_when_no_cargo(self):
        from crates.build import check_rust
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert check_rust() is False

    def test_check_rust_returns_true_when_cargo_found(self):
        from crates.build import check_rust
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "cargo 1.75.0"
        with patch("subprocess.run", return_value=mock_result):
            assert check_rust() is True

    def test_build_returns_false_without_rust(self):
        from crates.build import build
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert build() is False


# =============================================================================
# Rust source files exist
# =============================================================================

class TestRustSourceFiles:
    def test_cargo_toml_exists(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "Cargo.toml")
        assert os.path.isfile(path)

    def test_main_rs_exists(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "src", "main.rs")
        assert os.path.isfile(path)

    def test_vad_rs_exists(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "src", "vad.rs")
        assert os.path.isfile(path)

    def test_buffer_rs_exists(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "src", "buffer.rs")
        assert os.path.isfile(path)

    def test_cargo_toml_has_cpal(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "Cargo.toml")
        with open(path) as f:
            content = f.read()
        assert "cpal" in content

    def test_cargo_toml_has_serde_json(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "Cargo.toml")
        with open(path) as f:
            content = f.read()
        assert "serde_json" in content

    def test_main_rs_has_frame_output(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "src", "main.rs")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Utterance" in content
        assert "Frame" in content

    def test_vad_rs_has_speech_end(self):
        path = os.path.join(ROOT, "crates", "audio-capture", "src", "vad.rs")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "SpeechEnd" in content
        assert "hangover" in content.lower()


# =============================================================================
# Integration — imports and assistant_loop wiring
# =============================================================================

class TestIntegration:
    def test_rust_audio_unavailable_importable(self):
        from audio.rust_audio import RustAudioUnavailable
        assert issubclass(RustAudioUnavailable, RuntimeError)

    def test_module_imports_cleanly(self):
        import audio.rust_audio as ra
        assert hasattr(ra, "RustAudioCapture")
        assert hasattr(ra, "AudioFrame")
        assert hasattr(ra, "Utterance")
        assert hasattr(ra, "is_rust_audio_available")
        assert hasattr(ra, "get_audio_backend")
        assert hasattr(ra, "BINARY_PATH")

    def test_binary_path_is_absolute(self):
        assert os.path.isabs(BINARY_PATH)

    def test_binary_path_contains_bin_dir(self):
        assert os.sep + "bin" + os.sep in BINARY_PATH or "/bin/" in BINARY_PATH

    def test_assistant_loop_references_rust_audio(self):
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path) as f:
            src = f.read()
        assert "rust_audio" in src
        assert "is_rust_audio_available" in src

    def test_audio_init_exists(self):
        path = os.path.join(ROOT, "audio", "__init__.py")
        assert os.path.isfile(path)
