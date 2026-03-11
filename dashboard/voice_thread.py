"""
Voice Thread — voice-first I/O with barge-in (interruptible TTS).

Like Grok voice mode: if the user speaks while G is speaking,
G immediately stops talking and listens to the user first.

Uses speech.speak_interruptible() which monitors the mic during TTS
and halts on voice detection. The transcription is then passed
to the brain immediately.
"""

import logging
import queue
import threading

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class VoiceThread(QThread):
    """Voice-first thread with barge-in support."""

    transcriptionReady = pyqtSignal(str)
    listeningChanged = pyqtSignal(bool)
    speakingChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._speak_queue = queue.Queue()
        self._loaded = False
        self._listen_fn = None
        self._speak_fn = None
        self._stop_speak_fn = None

    def speak(self, text: str):
        """Queue text for TTS (will be interrupted if user speaks)."""
        self._speak_queue.put(text)

    def stop_listening(self):
        self._running = False
        self._speak_queue.put(None)
        # Stop any active speech
        if self._stop_speak_fn:
            try:
                self._stop_speak_fn()
            except Exception:
                pass
        self.wait(5000)

    def run(self):
        self._running = True
        self._load_speech()

        if not self._loaded:
            logger.error("Speech module not available")
            return

        # Speaker thread (processes TTS queue)
        speaker = threading.Thread(target=self._speaker_loop, daemon=True)
        speaker.start()

        # Main: listen loop (voice-first)
        while self._running:
            try:
                self.listeningChanged.emit(True)
                text = self._listen_fn()
                self.listeningChanged.emit(False)

                if text and self._running:
                    # Barge-in: stop any active speech immediately
                    if self._stop_speak_fn:
                        try:
                            self._stop_speak_fn()
                        except Exception:
                            pass
                    # Clear pending speech queue (user interrupted)
                    while not self._speak_queue.empty():
                        try:
                            self._speak_queue.get_nowait()
                        except queue.Empty:
                            break

                    logger.info(f"[Voice] Heard: {text}")
                    self.transcriptionReady.emit(text)
            except Exception as e:
                self.listeningChanged.emit(False)
                if self._running:
                    logger.error(f"Listen error: {e}")

        self.listeningChanged.emit(False)

    def _speaker_loop(self):
        """Process TTS queue — speaks with barge-in support."""
        while self._running:
            try:
                text = self._speak_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if text is None:
                break

            # Skip system messages and very short text
            if not text or len(text) < 2:
                continue

            try:
                self.speakingChanged.emit(True)
                # speak_interruptible monitors the mic and stops on voice detection
                result = self._speak_fn(text)
                self.speakingChanged.emit(False)

                # If speak was interrupted, the returned text is the user's speech
                if isinstance(result, str) and result:
                    logger.info(f"[Voice] Barge-in detected: {result}")
                    self.transcriptionReady.emit(result)
            except Exception as e:
                self.speakingChanged.emit(False)
                logger.error(f"Speak error: {e}")

    def _load_speech(self):
        """Import speech module (heavy, done on thread start)."""
        try:
            from speech import listen, speak_interruptible, stop_speaking
            self._listen_fn = listen
            self._speak_fn = speak_interruptible
            self._stop_speak_fn = stop_speaking
            self._loaded = True
            logger.info("Speech loaded (voice-first mode with barge-in)")
        except ImportError:
            # Fallback without barge-in
            try:
                from speech import listen, speak
                self._listen_fn = listen
                self._speak_fn = speak
                self._stop_speak_fn = None
                self._loaded = True
                logger.info("Speech loaded (basic mode, no barge-in)")
            except Exception as e:
                logger.error(f"Speech load failed completely: {e}")
