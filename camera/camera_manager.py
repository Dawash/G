"""
Camera Manager — discover, open, capture, and manage local and IP cameras.

Provides a unified interface for:
  - Local webcam discovery (probe indices 0-4 via cv2.VideoCapture)
  - IP camera support (RTSP/HTTP streams, CCTV)
  - Frame capture as numpy array, JPEG bytes, or saved file
  - Natural-language camera selection ("front camera", "kitchen", "CCTV")

All cv2 imports are inside try/except — the module degrades gracefully
when opencv-python is not installed.

Usage:
    from camera.camera_manager import camera_mgr
    cameras = camera_mgr.discover_cameras()
    camera_mgr.open_camera(0)
    frame = camera_mgr.capture_frame(0)
    camera_mgr.close_all()
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency: OpenCV
# ---------------------------------------------------------------------------
_cv2 = None
try:
    import cv2 as _cv2  # type: ignore
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Optional dependency: numpy (always present if cv2 is, but guard anyway)
# ---------------------------------------------------------------------------
_np = None
try:
    import numpy as _np  # type: ignore
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CameraInfo:
    """Metadata about a discovered or configured camera."""
    id: Union[int, str]                 # int for local index, str for IP URL
    name: str = ""                      # human-readable name
    type: str = "local"                 # "local" or "ip"
    url: str = ""                       # RTSP/HTTP URL (ip cameras only)
    resolution: Tuple[int, int] = (0, 0)
    is_open: bool = False
    is_front: bool = False              # True if this is a front-facing webcam
    location: str = ""                  # e.g. "kitchen", "front door", "office"

    def __str__(self) -> str:
        parts = [f"[{self.id}] {self.name or 'Camera'}"]
        if self.type == "ip":
            parts.append(f"({self.url})")
        if self.resolution != (0, 0):
            parts.append(f"{self.resolution[0]}x{self.resolution[1]}")
        if self.location:
            parts.append(f"@ {self.location}")
        if self.is_open:
            parts.append("[OPEN]")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Camera Manager
# ---------------------------------------------------------------------------

class CameraManager:
    """Unified camera management for local webcams and IP cameras."""

    _MAX_LOCAL_INDEX = 5  # probe indices 0..4

    def __init__(self):
        self._cameras: Dict[Union[int, str], CameraInfo] = {}
        self._captures: Dict[Union[int, str], Any] = {}  # id -> cv2.VideoCapture
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_cameras(self) -> List[CameraInfo]:
        """Probe local camera indices 0-4, return list of available CameraInfo."""
        if _cv2 is None:
            logger.debug("opencv-python not installed — camera discovery skipped")
            return []

        found: List[CameraInfo] = []
        for idx in range(self._MAX_LOCAL_INDEX):
            try:
                cap = _cv2.VideoCapture(idx, _cv2.CAP_DSHOW)
                if cap.isOpened():
                    w = int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    info = CameraInfo(
                        id=idx,
                        name=f"Camera {idx}" if idx > 0 else "Webcam",
                        type="local",
                        resolution=(w, h),
                        is_front=(idx == 0),  # assume index 0 is front-facing
                    )
                    with self._lock:
                        self._cameras[idx] = info
                    found.append(info)
                    logger.debug("Discovered local camera %d (%dx%d)", idx, w, h)
                else:
                    cap.release()
            except Exception as exc:
                logger.debug("Probe camera %d failed: %s", idx, exc)
        return found

    def list_cameras(self) -> str:
        """Return a human-readable listing of all known cameras."""
        with self._lock:
            if not self._cameras:
                return "No cameras discovered. Run discover_cameras() first or add IP cameras."
            lines = [f"Cameras ({len(self._cameras)}):"]
            for cam in self._cameras.values():
                lines.append(f"  {cam}")
            return "\n".join(lines)

    # ------------------------------------------------------------------
    # IP camera management
    # ------------------------------------------------------------------

    def add_ip_camera(
        self,
        name: str,
        url: str,
        location: str = "",
    ) -> CameraInfo:
        """Register an IP camera (RTSP/HTTP) for later use."""
        info = CameraInfo(
            id=url,
            name=name,
            type="ip",
            url=url,
            location=location,
        )
        with self._lock:
            self._cameras[url] = info
        logger.info("Added IP camera '%s' at %s", name, url)
        return info

    def load_ip_cameras_from_config(self, config: Dict[str, Any]) -> int:
        """Load IP cameras from the 'cameras' section of config.json.

        Expected format:
            "cameras": [
                {"name": "Front Door", "url": "rtsp://...", "location": "entrance"},
                ...
            ]

        Returns the number of cameras loaded.
        """
        cameras_list = config.get("cameras", [])
        if not isinstance(cameras_list, list):
            return 0
        count = 0
        for entry in cameras_list:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url", "")
            name = entry.get("name", f"IP Camera {count}")
            location = entry.get("location", "")
            if url:
                self.add_ip_camera(name, url, location)
                count += 1
        if count:
            logger.info("Loaded %d IP camera(s) from config", count)
        return count

    # ------------------------------------------------------------------
    # Open / Close
    # ------------------------------------------------------------------

    def open_camera(self, camera_id: Union[int, str]) -> str:
        """Open a camera by its id (local index or IP URL)."""
        if _cv2 is None:
            return "Error: opencv-python is not installed."

        with self._lock:
            if camera_id in self._captures:
                return f"Camera {camera_id} is already open."

            info = self._cameras.get(camera_id)
            if info is None:
                return f"Camera {camera_id} not found. Run discover_cameras() or add_ip_camera() first."

            try:
                if info.type == "local":
                    cap = _cv2.VideoCapture(camera_id, _cv2.CAP_DSHOW)
                else:
                    cap = _cv2.VideoCapture(info.url)

                if not cap.isOpened():
                    return f"Failed to open camera {camera_id}."

                # Update resolution
                w = int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(_cv2.CAP_PROP_FRAME_HEIGHT))
                info.resolution = (w, h)
                info.is_open = True
                self._captures[camera_id] = cap
                return f"Opened {info.name} ({w}x{h})"
            except Exception as exc:
                return f"Error opening camera {camera_id}: {exc}"

    def close_camera(self, camera_id: Union[int, str]) -> str:
        """Close a camera and release its resources."""
        with self._lock:
            cap = self._captures.pop(camera_id, None)
            if cap is None:
                return f"Camera {camera_id} is not open."
            try:
                cap.release()
            except Exception:
                pass
            info = self._cameras.get(camera_id)
            if info:
                info.is_open = False
            name = info.name if info else str(camera_id)
            return f"Closed {name}."

    def close_all(self) -> None:
        """Release all open cameras."""
        with self._lock:
            for cam_id, cap in list(self._captures.items()):
                try:
                    cap.release()
                except Exception:
                    pass
                info = self._cameras.get(cam_id)
                if info:
                    info.is_open = False
            self._captures.clear()
        logger.debug("All cameras closed")

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------

    def capture_frame(self, camera_id: Union[int, str]) -> Optional[Any]:
        """Capture a single frame as a numpy array (BGR).

        Opens the camera automatically if not already open.
        Returns None on failure.
        """
        if _cv2 is None or _np is None:
            logger.debug("cv2/numpy not available for capture_frame")
            return None

        with self._lock:
            cap = self._captures.get(camera_id)

        if cap is None:
            # Auto-open
            result = self.open_camera(camera_id)
            if "Error" in result or "Failed" in result or "not found" in result.lower():
                logger.warning("capture_frame: %s", result)
                return None
            with self._lock:
                cap = self._captures.get(camera_id)
            if cap is None:
                return None

        try:
            ret, frame = cap.read()
            if ret and frame is not None:
                return frame
            logger.debug("capture_frame: read() returned empty for camera %s", camera_id)
            return None
        except Exception as exc:
            logger.debug("capture_frame error: %s", exc)
            return None

    def capture_frame_as_bytes(self, camera_id: Union[int, str], quality: int = 85) -> Optional[bytes]:
        """Capture a frame and return it as JPEG bytes.

        Args:
            camera_id: Camera index or URL.
            quality: JPEG quality (1-100).

        Returns JPEG bytes or None on failure.
        """
        if _cv2 is None:
            return None
        frame = self.capture_frame(camera_id)
        if frame is None:
            return None
        try:
            ok, buf = _cv2.imencode(".jpg", frame, [_cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                return buf.tobytes()
        except Exception as exc:
            logger.debug("capture_frame_as_bytes encode error: %s", exc)
        return None

    def save_frame(self, camera_id: Union[int, str], filepath: str) -> str:
        """Capture and save a single frame to disk.

        Returns the absolute file path on success, or an error message.
        """
        if _cv2 is None:
            return "Error: opencv-python is not installed."
        frame = self.capture_frame(camera_id)
        if frame is None:
            return f"Error: could not capture frame from camera {camera_id}."
        try:
            abs_path = os.path.abspath(filepath)
            os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
            _cv2.imwrite(abs_path, frame)
            return abs_path
        except Exception as exc:
            return f"Error saving frame: {exc}"

    # ------------------------------------------------------------------
    # Natural-language camera selection
    # ------------------------------------------------------------------

    def find_camera_by_query(self, query: str) -> Optional[CameraInfo]:
        """Find a camera matching a natural-language description.

        Matches against: name, location, type keywords.
        Examples: "front camera", "kitchen", "CCTV", "webcam", "0"
        """
        if not query:
            # Default: return first available camera
            with self._lock:
                if self._cameras:
                    return next(iter(self._cameras.values()))
            return None

        q = query.strip().lower()

        # Direct ID match (integer index)
        try:
            idx = int(q)
            with self._lock:
                if idx in self._cameras:
                    return self._cameras[idx]
        except ValueError:
            pass

        # Direct URL match
        with self._lock:
            if q in self._cameras:
                return self._cameras[q]

        # Keyword matching
        candidates: List[Tuple[int, CameraInfo]] = []
        with self._lock:
            for cam in self._cameras.values():
                score = 0
                search_text = f"{cam.name} {cam.location} {cam.type} {cam.url}".lower()

                # Exact substring
                if q in search_text:
                    score += 10

                # Token overlap
                q_tokens = set(q.split())
                s_tokens = set(search_text.split())
                overlap = q_tokens & s_tokens
                score += len(overlap) * 3

                # Special keywords
                if "front" in q and cam.is_front:
                    score += 5
                if "webcam" in q and cam.type == "local":
                    score += 5
                if "cctv" in q and cam.type == "ip":
                    score += 5
                if "ip" in q and cam.type == "ip":
                    score += 3

                if score > 0:
                    candidates.append((score, cam))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

        # Fallback: return first camera
        with self._lock:
            if self._cameras:
                return next(iter(self._cameras.values()))
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_open_cameras(self) -> List[CameraInfo]:
        """Return list of currently open cameras."""
        with self._lock:
            return [c for c in self._cameras.values() if c.is_open]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

camera_mgr = CameraManager()
