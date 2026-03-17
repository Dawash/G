"""Rust-backed audio capture + VAD for the G voice assistant.

Provides a Python wrapper around the compiled Rust binary
(`audio/bin/audio_capture[.exe]`). Falls back gracefully when
the binary is not present — Python's existing VAD pipeline is used.

Quick-start::

    from audio.rust_audio import RustAudioCapture, is_rust_audio_available

    if is_rust_audio_available():
        cap = RustAudioCapture()
        cap.start()
        utt = cap.get_utterance(timeout=5.0)
        cap.stop()
"""
