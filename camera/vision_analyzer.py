"""
Vision Analyzer — send camera frames to a vision LLM for analysis.

Supports:
  - Ollama llava (local, default)
  - OpenAI GPT-4o / GPT-4-turbo (via model_router vision tier)
  - Anthropic Claude (via model_router vision tier)

Features:
  - General scene description
  - Object identification
  - People counting
  - OCR-like text reading
  - Frame comparison (change detection)

Images are resized to max 1024px before sending to the LLM to reduce
latency and token usage.

Usage:
    from camera.vision_analyzer import vision
    frame = camera_mgr.capture_frame(0)
    description = vision.describe_scene(frame)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
_cv2 = None
try:
    import cv2 as _cv2  # type: ignore
except ImportError:
    pass

_np = None
try:
    import numpy as _np  # type: ignore
except ImportError:
    pass

_PIL = None
try:
    from PIL import Image as _PIL  # type: ignore
except ImportError:
    pass

# Max dimension before sending to LLM
_MAX_IMAGE_DIM = 1024
_VISION_MODEL = "llava"
_VISION_TIMEOUT = 60


class VisionAnalyzer:
    """Analyze camera frames using a vision LLM."""

    def __init__(self):
        self._ollama_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_frame(self, frame: Any, question: str) -> str:
        """Send a frame to the vision LLM with a custom question.

        Args:
            frame: numpy array (BGR from OpenCV) or PIL Image.
            question: What to ask about the image.

        Returns:
            LLM response string, or error message.
        """
        b64 = self._frame_to_base64(frame)
        if b64 is None:
            return "Error: could not encode frame to base64."
        return self._analyze(b64, question)

    def describe_scene(self, frame: Any) -> str:
        """Generate a general description of the scene."""
        return self.analyze_frame(
            frame,
            "Describe what you see in this image in 2-3 sentences. "
            "Include notable objects, people, and the general setting."
        )

    def identify_objects(self, frame: Any) -> str:
        """List the main objects visible in the frame."""
        return self.analyze_frame(
            frame,
            "List the main objects visible in this image. "
            "Return a comma-separated list of objects, nothing else."
        )

    def count_people(self, frame: Any) -> str:
        """Count people in the frame and briefly describe them."""
        return self.analyze_frame(
            frame,
            "How many people are visible in this image? "
            "For each person, briefly describe what they are doing. "
            "If no people, say 'No people visible.'"
        )

    def read_text(self, frame: Any) -> str:
        """Extract any visible text from the frame (OCR-like)."""
        return self.analyze_frame(
            frame,
            "Read and transcribe all visible text in this image. "
            "If there are signs, labels, or screens with text, include all of them. "
            "If no text is visible, say 'No text visible.'"
        )

    def compare_frames(self, frame1: Any, frame2: Any) -> str:
        """Describe the differences between two frames.

        Sends both frames in a single prompt to identify changes.
        """
        b64_1 = self._frame_to_base64(frame1)
        b64_2 = self._frame_to_base64(frame2)
        if b64_1 is None or b64_2 is None:
            return "Error: could not encode one or both frames."

        # For Ollama llava, send both images in the images array
        question = (
            "Compare these two images and describe what changed between them. "
            "Focus on differences in objects, people, or scene layout."
        )
        return self._analyze_with_ollama(
            [b64_1, b64_2],
            question,
        )

    # ------------------------------------------------------------------
    # Internal: frame encoding
    # ------------------------------------------------------------------

    def _frame_to_base64(self, frame: Any) -> Optional[str]:
        """Convert a numpy array (BGR) or PIL Image to base64-encoded JPEG.

        Resizes to max _MAX_IMAGE_DIM on the longest side.
        """
        if frame is None:
            return None

        # Handle PIL Image
        if _PIL is not None and isinstance(frame, _PIL.Image):
            frame = self._resize_pil(frame)
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("utf-8")

        # Handle numpy array (OpenCV BGR)
        if _cv2 is not None and _np is not None:
            if hasattr(frame, "shape") and len(frame.shape) >= 2:
                frame = self._resize_cv2(frame)
                ok, buf = _cv2.imencode(".jpg", frame, [_cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    return base64.b64encode(buf.tobytes()).decode("utf-8")

        logger.debug("_frame_to_base64: unsupported frame type %s", type(frame))
        return None

    def _resize_cv2(self, frame: Any) -> Any:
        """Resize a cv2 frame so the longest side is at most _MAX_IMAGE_DIM."""
        h, w = frame.shape[:2]
        if max(h, w) <= _MAX_IMAGE_DIM:
            return frame
        scale = _MAX_IMAGE_DIM / max(h, w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        return _cv2.resize(frame, (new_w, new_h), interpolation=_cv2.INTER_AREA)

    def _resize_pil(self, img: Any) -> Any:
        """Resize a PIL Image so the longest side is at most _MAX_IMAGE_DIM."""
        w, h = img.size
        if max(h, w) <= _MAX_IMAGE_DIM:
            return img
        scale = _MAX_IMAGE_DIM / max(h, w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        return img.resize((new_w, new_h), _PIL.LANCZOS if hasattr(_PIL, "LANCZOS") else 1)

    # ------------------------------------------------------------------
    # Internal: LLM backends
    # ------------------------------------------------------------------

    def _analyze(self, b64: str, question: str) -> str:
        """Route analysis to the best available vision backend."""
        # Try model_router vision tier first (cloud providers)
        result = self._analyze_with_router(b64, question)
        if result:
            return result

        # Fallback to Ollama llava
        result = self._analyze_with_ollama([b64], question)
        if result:
            return result

        return "Error: no vision model available. Install Ollama + llava, or configure a cloud provider."

    def _analyze_with_router(self, b64: str, question: str) -> Optional[str]:
        """Try analysis via the model_router vision tier."""
        try:
            from llm.model_router import model_router
            cfg = model_router.pool.get_best("vision")
            if cfg is None:
                return None

            if cfg.provider == "openai":
                return self._call_openai_vision(cfg, b64, question)
            elif cfg.provider == "anthropic":
                return self._call_anthropic_vision(cfg, b64, question)
            elif cfg.provider == "ollama":
                # Route to ollama handler
                return self._analyze_with_ollama([b64], question, model=cfg.model)
            else:
                return None
        except Exception as exc:
            logger.debug("model_router vision failed: %s", exc)
            return None

    def _analyze_with_ollama(
        self,
        images_b64: list,
        question: str,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """Send image(s) to Ollama /api/generate with the vision model."""
        try:
            import requests
        except ImportError:
            return None

        url = self._get_ollama_url()
        payload = {
            "model": model or _VISION_MODEL,
            "prompt": question,
            "images": images_b64,
            "stream": False,
            "options": {"temperature": 0.3},
        }

        try:
            resp = requests.post(
                f"{url}/api/generate",
                json=payload,
                timeout=_VISION_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", "").strip()
            logger.debug("Ollama vision returned %d: %s", resp.status_code, resp.text[:200])
            return None
        except Exception as exc:
            logger.debug("Ollama vision error: %s", exc)
            return None

    def _call_openai_vision(self, cfg: Any, b64: str, question: str) -> Optional[str]:
        """Call OpenAI vision endpoint."""
        try:
            import requests
            headers = {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": cfg.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "low",
                        }},
                    ],
                }],
                "max_tokens": 512,
                "temperature": 0.3,
            }
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            return None
        except Exception as exc:
            logger.debug("OpenAI vision error: %s", exc)
            return None

    def _call_anthropic_vision(self, cfg: Any, b64: str, question: str) -> Optional[str]:
        """Call Anthropic Claude vision endpoint."""
        try:
            import requests
            headers = {
                "x-api-key": cfg.api_key,
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": cfg.model,
                "max_tokens": 512,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        }},
                        {"type": "text", "text": question},
                    ],
                }],
            }
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"].strip()
            return None
        except Exception as exc:
            logger.debug("Anthropic vision error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_ollama_url(self) -> str:
        """Get Ollama URL from config, cached."""
        if self._ollama_url is None:
            try:
                from config import load_config, DEFAULT_OLLAMA_URL
                cfg = load_config()
                self._ollama_url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
            except Exception:
                self._ollama_url = "http://localhost:11434"
        return self._ollama_url


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

vision = VisionAnalyzer()
