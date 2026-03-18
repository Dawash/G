"""
Camera Perception — real-time face & gesture detection via webcam.

Features:
  - Face detection via OpenCV Haar cascade (built-in, no extra deps)
  - Hand gesture detection via mediapipe (optional, try/except)
  - Publishes to event bus: perception.camera.user_present,
    perception.camera.user_left, perception.camera.gesture
  - Updates awareness.user_present on state changes
  - NEVER saves images to disk

Disabled by default.  Enable in config.json:
    "camera_enabled": true
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency imports
# ---------------------------------------------------------------------------

_cv2 = None
try:
    import cv2 as _cv2  # type: ignore
except ImportError:
    pass

_mp_hands = None
_mp_drawing = None
try:
    import mediapipe as _mp  # type: ignore
    _mp_hands = _mp.solutions.hands
    _mp_drawing = _mp.solutions.drawing_utils
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum seconds between event bus publishes of the same topic
_DEBOUNCE_SECS = 2.0

# How many consecutive empty frames before we declare "user left"
_ABSENT_FRAME_THRESHOLD = 15

# How many consecutive face frames before we declare "user present"
_PRESENT_FRAME_THRESHOLD = 3


class CameraPerception:
    """Background webcam capture with face/gesture detection.

    Parameters
    ----------
    camera_index : int
        OS camera index (0 = default webcam).
    fps : float
        Target capture frames per second (approximate).
    """

    def __init__(self, camera_index: int = 0, fps: float = 5.0):
        self._camera_index = camera_index
        self._fps = max(1.0, fps)
        self._interval = 1.0 / self._fps

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Detection state
        self._user_present = False
        self._consecutive_present = 0
        self._consecutive_absent = 0
        self._last_gesture: Optional[str] = None
        self._last_gesture_time = 0.0

        # Debounce timestamps per topic
        self._last_publish: Dict[str, float] = {}

        # Detectors (lazy init)
        self._face_cascade = None
        self._hands = None

        # Stats
        self._frames_processed = 0
        self._faces_detected = 0
        self._gestures_detected = 0
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Return True if cv2 is installed and the camera can be opened."""
        if _cv2 is None:
            return False
        try:
            cap = _cv2.VideoCapture(self._camera_index, _cv2.CAP_DSHOW)
            ok = cap.isOpened()
            cap.release()
            return ok
        except Exception:
            return False

    def start(self) -> bool:
        """Start the background capture thread.  Returns True on success."""
        if _cv2 is None:
            logger.warning("Camera perception requires opencv-python (cv2)")
            return False
        if self._thread and self._thread.is_alive():
            return True  # already running

        self._stop_event.clear()
        self._init_detectors()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="camera-perception",
            daemon=True,
        )
        self._thread.start()
        self._started_at = time.time()
        self._publish("perception.camera.started", {})
        logger.info("Camera perception started (index=%d, fps=%.1f)", self._camera_index, self._fps)
        return True

    def stop(self) -> None:
        """Stop capture and release camera."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._hands:
            try:
                self._hands.close()
            except Exception:
                pass
            self._hands = None
        self._publish("perception.camera.stopped", {})
        logger.info("Camera perception stopped (frames=%d)", self._frames_processed)

    def status(self) -> Dict[str, Any]:
        """Return observability snapshot."""
        running = self._thread is not None and self._thread.is_alive()
        uptime = time.time() - self._started_at if self._started_at and running else 0
        return {
            "running": running,
            "camera_index": self._camera_index,
            "fps_target": self._fps,
            "frames_processed": self._frames_processed,
            "faces_detected": self._faces_detected,
            "gestures_detected": self._gestures_detected,
            "user_present": self._user_present,
            "uptime_seconds": round(uptime, 1),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _init_detectors(self) -> None:
        """Lazy-load Haar cascade and mediapipe hands."""
        if _cv2 is not None and self._face_cascade is None:
            try:
                cascade_path = _cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                self._face_cascade = _cv2.CascadeClassifier(cascade_path)
                if self._face_cascade.empty():
                    logger.warning("Haar cascade failed to load from %s", cascade_path)
                    self._face_cascade = None
            except Exception as exc:
                logger.debug("Face cascade init failed: %s", exc)
                self._face_cascade = None

        if _mp_hands is not None and self._hands is None:
            try:
                self._hands = _mp_hands.Hands(
                    static_image_mode=False,
                    max_num_hands=1,
                    min_detection_confidence=0.6,
                    min_tracking_confidence=0.5,
                )
            except Exception as exc:
                logger.debug("MediaPipe hands init failed: %s", exc)
                self._hands = None

    def _capture_loop(self) -> None:
        """Main background loop — runs at configured FPS."""
        cap = _cv2.VideoCapture(self._camera_index, _cv2.CAP_DSHOW)
        if not cap.isOpened():
            logger.error("Cannot open camera %d", self._camera_index)
            return
        try:
            while not self._stop_event.is_set():
                loop_start = time.monotonic()

                ret, frame = cap.read()
                if not ret:
                    time.sleep(self._interval)
                    continue

                events = self._analyze_frame(frame)
                for topic, data in events:
                    self._publish(topic, data)

                self._frames_processed += 1

                # Pace to target FPS
                elapsed = time.monotonic() - loop_start
                remaining = self._interval - elapsed
                if remaining > 0:
                    self._stop_event.wait(remaining)
        except Exception as exc:
            logger.error("Camera capture loop error: %s", exc)
        finally:
            cap.release()

    def _analyze_frame(self, frame) -> List[Tuple[str, Dict[str, Any]]]:
        """Run detectors on a single frame.

        Returns a list of (topic, payload) tuples to publish.
        No images are ever saved to disk.
        """
        events: List[Tuple[str, Dict[str, Any]]] = []
        gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)

        # ── Face detection ────────────────────────────────────────────
        face_found = False
        if self._face_cascade is not None:
            faces = self._face_cascade.detectMultiScale(
                gray, scaleFactor=1.3, minNeighbors=5, minSize=(60, 60),
            )
            if len(faces) > 0:
                face_found = True
                self._faces_detected += 1

        # ── Presence state machine ────────────────────────────────────
        if face_found:
            self._consecutive_absent = 0
            self._consecutive_present += 1
            if not self._user_present and self._consecutive_present >= _PRESENT_FRAME_THRESHOLD:
                self._user_present = True
                events.append(("perception.camera.user_present", {"present": True}))
                self._update_awareness(user_present=True)
        else:
            self._consecutive_present = 0
            self._consecutive_absent += 1
            if self._user_present and self._consecutive_absent >= _ABSENT_FRAME_THRESHOLD:
                self._user_present = False
                events.append(("perception.camera.user_present", {"present": False}))
                self._update_awareness(user_present=False)

        # ── Hand gesture detection (mediapipe) ────────────────────────
        if self._hands is not None:
            rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
            try:
                result = self._hands.process(rgb)
                if result and result.multi_hand_landmarks:
                    for hand_lm in result.multi_hand_landmarks:
                        gesture = self._classify_gesture(hand_lm.landmark)
                        if gesture:
                            now = time.time()
                            # Debounce same gesture within 2s
                            if gesture != self._last_gesture or (now - self._last_gesture_time) > _DEBOUNCE_SECS:
                                self._last_gesture = gesture
                                self._last_gesture_time = now
                                self._gestures_detected += 1
                                events.append(("perception.camera.gesture", {"gesture": gesture}))
            except Exception as exc:
                logger.debug("Gesture detection error: %s", exc)

        return events

    def _classify_gesture(self, landmarks) -> Optional[str]:
        """Classify a hand gesture from mediapipe landmarks.

        Supported gestures:
          - open_palm  (all fingers extended)
          - fist       (all fingers closed)
          - thumbs_up  (thumb up, other fingers closed)
          - peace      (index + middle extended, rest closed)

        Returns None if no known gesture is detected.
        """
        if not landmarks or len(landmarks) < 21:
            return None

        # Finger tip and pip (proximal interphalangeal) landmark indices
        # Tip IDs: thumb=4, index=8, middle=12, ring=16, pinky=20
        # PIP IDs: thumb=3, index=6, middle=10, ring=14, pinky=18
        tips = [4, 8, 12, 16, 20]
        pips = [3, 6, 10, 14, 18]

        fingers_up = []
        for tip_id, pip_id in zip(tips, pips):
            tip = landmarks[tip_id]
            pip = landmarks[pip_id]
            # Finger is "up" if tip is above pip (lower y = higher on screen)
            fingers_up.append(tip.y < pip.y)

        # Thumb uses x-axis (tip further from palm center = extended)
        wrist = landmarks[0]
        thumb_tip = landmarks[4]
        thumb_ip = landmarks[3]
        thumb_extended = abs(thumb_tip.x - wrist.x) > abs(thumb_ip.x - wrist.x)
        fingers_up[0] = thumb_extended

        up_count = sum(fingers_up)

        if up_count == 5:
            return "open_palm"
        if up_count == 0:
            return "fist"
        if fingers_up[0] and up_count == 1:
            return "thumbs_up"
        if fingers_up[1] and fingers_up[2] and up_count == 2:
            return "peace"

        return None

    def _update_awareness(self, **kwargs: Any) -> None:
        """Update the shared awareness state (if available)."""
        try:
            from core.awareness_state import awareness
            awareness.update(**kwargs)
        except Exception:
            pass

    def _publish(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish an event on the bus with debounce."""
        now = time.time()
        last = self._last_publish.get(topic, 0.0)
        if now - last < _DEBOUNCE_SECS and topic not in ("perception.camera.started", "perception.camera.stopped"):
            return
        self._last_publish[topic] = now
        try:
            from core.event_bus import bus
            bus.publish(topic, payload, source="camera_perception")
        except Exception:
            pass
