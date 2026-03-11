"""
Screen Vision — screenshot capture + AI visual analysis via Ollama llava.

Gives the Brain eyes: it can see the screen, find UI elements,
detect blocking dialogs/popups, and verify if actions succeeded.

Uses Ollama's /api/generate endpoint with llava (vision model) to
analyze screenshots locally — images never leave the machine.

Requirements:
  - Pillow (pip install Pillow)
  - Ollama running with llava model pulled (ollama pull llava)
  - pyautogui for screenshots
"""

import base64
import io
import json
import logging
import requests

logger = logging.getLogger(__name__)

# ===================================================================
# Configuration
# ===================================================================

VISION_MODEL = "llava"


def _get_ollama_url():
    """Get the Ollama URL from config, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_URL
        cfg = load_config()
        return cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    except Exception:
        return "http://localhost:11434"


OLLAMA_API = _get_ollama_url()
MAX_SCREENSHOT_WIDTH = 1280  # Resize for faster processing
VISION_TIMEOUT = 60  # llava can be slow on first load


# ===================================================================
# Screenshot capture
# ===================================================================

def capture_screenshot(region=None):
    """
    Take a screenshot, resize to MAX_SCREENSHOT_WIDTH for speed.
    Returns a PIL Image or None on failure.

    Args:
        region: Optional (x, y, width, height) tuple to capture a region.
    """
    try:
        import pyautogui
        from PIL import Image

        if region:
            img = pyautogui.screenshot(region=region)
        else:
            img = pyautogui.screenshot()

        # Convert to PIL Image if needed (pyautogui returns PIL Image on Windows)
        if not isinstance(img, Image.Image):
            img = Image.frombytes("RGB", img.size, img.tobytes())

        # Resize for faster llava processing
        if img.width > MAX_SCREENSHOT_WIDTH:
            ratio = MAX_SCREENSHOT_WIDTH / img.width
            new_height = int(img.height * ratio)
            img = img.resize((MAX_SCREENSHOT_WIDTH, new_height), Image.LANCZOS)

        return img

    except Exception as e:
        logger.error(f"Screenshot capture failed: {e}")
        return None


def image_to_base64(image, fmt="PNG"):
    """Convert a PIL Image to base64 string (raw, no data URI prefix)."""
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ===================================================================
# Ollama Vision API (llava)
# ===================================================================

def _call_llava(prompt, image_b64, temperature=0.1, num_predict=300):
    """
    Call Ollama's /api/generate with a vision model.
    Uses the generate endpoint (NOT /v1/chat/completions — that doesn't
    support images in Ollama).

    Returns the response text or None on failure.
    """
    payload = {
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        },
    }

    try:
        resp = requests.post(
            f"{OLLAMA_API}/api/generate",
            json=payload,
            timeout=VISION_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "").strip()

    except requests.ConnectionError:
        logger.error("Cannot connect to Ollama for vision. Is it running?")
        return None
    except requests.Timeout:
        logger.error("Vision request timed out (llava may still be loading)")
        return None
    except Exception as e:
        logger.error(f"Vision API error: {e}")
        return None


# ===================================================================
# High-level vision functions
# ===================================================================

def analyze_screen(prompt, image=None):
    """
    Take a screenshot and ask llava about it.

    Args:
        prompt: Question about the screen (e.g., "What applications are visible?")
        image: Optional pre-captured PIL Image. If None, takes a new screenshot.

    Returns: Text description from llava, or error string.
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return "Error: could not capture screenshot."

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, num_predict=500)
    if result is None:
        return "Error: vision model did not respond. Is Ollama running with llava?"
    return result


def find_element(description, image=None):
    """
    Ask llava to locate a UI element on screen.

    Args:
        description: What to find (e.g., "the OK button", "the search bar")
        image: Optional pre-captured PIL Image.

    Returns: dict with {found, x, y, description} — coordinates scaled
             to actual screen resolution.
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return {"found": False, "x": 0, "y": 0, "description": "Could not capture screenshot"}

    # Remember resize ratio for coordinate scaling
    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    img_w, img_h = image.size
    scale_x = screen_w / img_w
    scale_y = screen_h / img_h

    prompt = (
        f"Find the UI element: '{description}' on this screenshot. "
        f"The image is {img_w}x{img_h} pixels. "
        f"If you can find it, respond with ONLY a JSON object: "
        f'{{"found": true, "x": <center_x>, "y": <center_y>, "description": "<what you see>"}}. '
        f"If you cannot find it, respond with: "
        f'{{"found": false, "x": 0, "y": 0, "description": "<why not found>"}}. '
        f"Respond with ONLY the JSON, no other text."
    )

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, temperature=0.1, num_predict=200)

    if result is None:
        return {"found": False, "x": 0, "y": 0, "description": "Vision model error"}

    # Parse JSON from response
    try:
        # Try to extract JSON from the response
        import re
        json_match = re.search(r'\{[^}]*\}', result)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(result)

        # Scale coordinates back to actual screen resolution
        if data.get("found"):
            data["x"] = int(data.get("x", 0) * scale_x)
            data["y"] = int(data.get("y", 0) * scale_y)

        return data

    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Could not parse find_element response: {result}")
        return {"found": False, "x": 0, "y": 0, "description": result}


def find_text_on_screen(text_to_find, image=None):
    """
    Find specific text on screen by asking llava to locate it.

    Unlike find_element (which finds UI elements by description),
    this function finds EXACT TEXT visible on screen — useful for
    clicking links, buttons, or menu items by their displayed text.

    Args:
        text_to_find: The exact text to locate (e.g. "Skip Ad", "Blinding Lights")
        image: Optional pre-captured PIL Image.

    Returns: dict with {found, x, y, confidence, context}
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return {"found": False, "x": 0, "y": 0, "confidence": 0, "context": "No screenshot"}

    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    img_w, img_h = image.size
    scale_x = screen_w / img_w
    scale_y = screen_h / img_h

    prompt = (
        f"I need to find the text \"{text_to_find}\" on this screenshot.\n"
        f"The image is {img_w}x{img_h} pixels.\n"
        f"Look carefully for this exact text or very similar text.\n"
        f"If found, respond with ONLY: {{\"found\": true, \"x\": <center_x>, \"y\": <center_y>}}\n"
        f"If NOT found, respond with: {{\"found\": false, \"x\": 0, \"y\": 0}}\n"
        f"Respond with ONLY JSON."
    )

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, temperature=0.1, num_predict=100)

    if result is None:
        return {"found": False, "x": 0, "y": 0, "confidence": 0, "context": "Vision error"}

    try:
        import re
        json_match = re.search(r'\{[^}]*\}', result)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(result)

        if data.get("found"):
            data["x"] = int(data.get("x", 0) * scale_x)
            data["y"] = int(data.get("y", 0) * scale_y)
            data["confidence"] = 0.8
            data["context"] = f"Found '{text_to_find}' via vision"
        else:
            data["confidence"] = 0
            data["context"] = "Text not visible on screen"

        return data

    except (json.JSONDecodeError, ValueError):
        return {"found": False, "x": 0, "y": 0, "confidence": 0, "context": result}


def describe_screen_region(x1, y1, x2, y2, question="What is in this region?", image=None):
    """
    Analyze a specific region of the screen.

    Crops the screenshot to the specified region and asks llava about it.
    Useful for understanding what's in a specific area without processing
    the entire screen.

    Args:
        x1, y1, x2, y2: Bounding box coordinates (screen pixels)
        question: What to ask about the region
        image: Optional pre-captured PIL Image

    Returns: description string
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return "Could not capture screenshot"

    try:
        import pyautogui
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    img_w, img_h = image.size
    # Scale screen coordinates to image coordinates
    ix1 = int(x1 * img_w / screen_w)
    iy1 = int(y1 * img_h / screen_h)
    ix2 = int(x2 * img_w / screen_w)
    iy2 = int(y2 * img_h / screen_h)

    # Crop the region
    try:
        cropped = image.crop((ix1, iy1, ix2, iy2))
        if cropped.size[0] < 10 or cropped.size[1] < 10:
            return "Region too small to analyze"
    except Exception as e:
        return f"Could not crop region: {e}"

    b64 = image_to_base64(cropped)
    result = _call_llava(question, b64, temperature=0.1, num_predict=200)
    return result or "Vision model did not respond"


def check_for_blockers(image=None):
    """
    Check if any dialog, popup, or error is blocking the screen.

    Returns: dict with {blocked, blocker_type, description, suggestion}
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return {"blocked": False, "blocker_type": "none",
                "description": "Could not capture screenshot", "suggestion": ""}

    prompt = (
        "Look at this screenshot. Is there any dialog box, popup, "
        "error message, cookie banner, profile picker, or modal window blocking "
        "the main content?\n"
        "Start your answer with YES or NO.\n"
        "If YES, describe what is blocking and how to dismiss it "
        "(e.g. 'click OK button', 'press Enter', 'click X')."
    )

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, temperature=0.1, num_predict=150)

    if result is None:
        return {"blocked": False, "blocker_type": "none",
                "description": "Vision model error", "suggestion": ""}

    result_lower = result.strip().lower()
    blocked = result_lower.startswith("yes")

    # Try to extract suggestion from the text
    suggestion = ""
    if blocked:
        import re
        m = re.search(r'(?:click|press|dismiss|close|tap)\s+[^.]+', result, re.I)
        if m:
            suggestion = m.group(0).strip()

    return {
        "blocked": blocked,
        "blocker_type": "dialog" if blocked else "none",
        "description": result.strip(),
        "suggestion": suggestion,
    }


def verify_action(expected_outcome, image=None):
    """
    Check if the screen matches an expected state after an action.

    Args:
        expected_outcome: What should be visible (e.g., "YouTube search results for ACDC")

    Returns: dict with {success, description, matches_expected}
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return {"success": False, "description": "Could not capture screenshot",
                "matches_expected": False}

    prompt = (
        f"Look at this screenshot. The expected outcome is: '{expected_outcome}'. "
        f"Does the screen ROUGHLY match? Be LENIENT:\n"
        f"- A website open in a browser counts as 'app is open'\n"
        f"- Any relevant content visible counts as success\n"
        f"- The right window in the foreground counts as success\n"
        f"Only say NO if clearly wrong or nothing happened.\n"
        f"Start your answer with YES or NO, then briefly describe what you see."
    )

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, temperature=0.1, num_predict=150)

    if result is None:
        return {"success": False, "description": "Vision model error",
                "matches_expected": False}

    # Parse YES/NO from the start of the response
    result_lower = result.strip().lower()
    matched = result_lower.startswith("yes")

    return {
        "success": matched,
        "description": result.strip(),
        "matches_expected": matched,
    }


def describe_screen_state(goal_context="", image=None):
    """
    Get a comprehensive description of the current screen state for
    agentic decision-making.

    Args:
        goal_context: What the agent is trying to do (for better analysis).
        image: Optional pre-captured PIL Image.

    Returns: dict with {foreground_app, state, description, blockers,
             available_actions, ready_for_task}
    """
    if image is None:
        image = capture_screenshot()
    if image is None:
        return {
            "foreground_app": "unknown",
            "state": "error",
            "description": "Could not capture screenshot",
            "blockers": [],
            "available_actions": [],
            "ready_for_task": False,
        }

    context_line = ""
    if goal_context:
        context_line = f"The user is trying to: {goal_context}\n"

    prompt = (
        f"{context_line}"
        "Describe this Windows screenshot in ONE short paragraph:\n"
        "1. What app/window is in the foreground?\n"
        "2. Is there any popup, dialog, banner, or error blocking?\n"
        "3. What state is the screen in? (normal, loading, error, dialog)\n"
        "4. If blocked, how to dismiss it? (e.g. click button, press key)\n"
        "Be specific and concise."
    )

    b64 = image_to_base64(image)
    result = _call_llava(prompt, b64, temperature=0.1, num_predict=250)

    if result is None:
        return {
            "foreground_app": "unknown",
            "state": "error",
            "description": "Vision model did not respond",
            "blockers": [],
            "available_actions": [],
            "ready_for_task": False,
        }

    description = result.strip()
    desc_lower = description.lower()

    # Detect blockers from the description
    blockers = []
    blocker_keywords = [
        "popup", "dialog", "modal", "banner", "alert", "error",
        "profile picker", "profile selector", "choose profile",
        "default browser", "not your default", "set as default",
        "cookie", "consent", "permission", "sign in", "log in",
        "update", "restart required", "uac", "administrator",
    ]
    for kw in blocker_keywords:
        if kw in desc_lower:
            blockers.append(kw)

    # Detect available actions
    import re
    actions = []
    action_patterns = [
        r'click\s+(?:the\s+|on\s+)?["\']?([^"\',.]+)',
        r'press\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s*(?:key|button)?',
        r'close\s+(?:the\s+)?([^,.]+)',
        r'dismiss\s+(?:the\s+)?([^,.]+)',
    ]
    for pat in action_patterns:
        for m in re.finditer(pat, description, re.I):
            actions.append(m.group(0).strip())

    # Determine if screen is ready for the task
    blocked = len(blockers) > 0
    state = "blocked" if blocked else "normal"
    if "loading" in desc_lower or "wait" in desc_lower:
        state = "loading"
    if "error" in desc_lower:
        state = "error"

    return {
        "foreground_app": _extract_app_name(description),
        "state": state,
        "description": description,
        "blockers": blockers,
        "available_actions": actions,
        "ready_for_task": not blocked,
    }


def _extract_app_name(description):
    """Extract the foreground app name from a screen description."""
    import re
    desc_lower = description.lower()

    # Common app patterns
    app_names = [
        "firefox", "chrome", "edge", "brave", "opera",
        "notepad", "file explorer", "explorer", "settings",
        "task manager", "calculator", "terminal", "powershell",
        "cmd", "command prompt", "vs code", "visual studio",
        "spotify", "discord", "steam", "word", "excel",
        "outlook", "teams", "paint", "photos", "vlc",
    ]
    for app in app_names:
        if app in desc_lower:
            return app.title()

    # Try to extract from "X is open" or "X window" patterns
    m = re.search(r'(?:the\s+)?(\w+(?:\s+\w+)?)\s+(?:window|app|application|is\s+open|is\s+in\s+the\s+foreground)', description, re.I)
    if m:
        return m.group(1).strip()

    return "unknown"


def get_active_window_title():
    """Get the title of the currently active/focused window."""
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win:
            return win.title
    except Exception:
        pass
    return ""


# ===================================================================
# Model availability checks
# ===================================================================

def is_vision_available():
    """Check if the llava model is available in Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_API}/api/tags", timeout=5)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        for m in models:
            name = m.get("name", "").split(":")[0]
            if name == VISION_MODEL:
                return True
        return False
    except Exception:
        return False


def ensure_vision_model():
    """
    Check if the vision model is available.
    Returns (available: bool, message: str).
    Does NOT auto-pull — just tells the user what to do.
    """
    try:
        resp = requests.get(f"{OLLAMA_API}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.ConnectionError:
        return False, "Ollama is not running. Start it with: ollama serve"
    except Exception as e:
        return False, f"Cannot check Ollama models: {e}"

    models = resp.json().get("models", [])
    for m in models:
        name = m.get("name", "").split(":")[0]
        if name == VISION_MODEL:
            return True, f"Vision model '{VISION_MODEL}' is ready."

    return False, (
        f"Vision model '{VISION_MODEL}' is not installed. "
        f"Pull it with: ollama pull {VISION_MODEL}"
    )
