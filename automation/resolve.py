"""
Target resolver — tiered strategy for finding UI targets.

Resolution order (most reliable → least reliable):
  1. UIA accessibility tree (find_control)
  2. Keyboard shortcut (app-specific hotkeys)
  3. Vision / screenshot (llava model)
  4. Coordinate fallback

Returns a ResolvedTarget with source, confidence, and recommended action.
"""

import logging

logger = logging.getLogger(__name__)


class ResolvedTarget:
    """Result of resolving a UI target description."""

    __slots__ = ("found", "source", "confidence", "name", "type",
                 "x", "y", "width", "height",
                 "action", "error")

    def __init__(self, found=False, source="none", confidence=0.0,
                 name="", type="", x=0, y=0, width=0, height=0,
                 action="click", error=""):
        self.found = found
        self.source = source      # "uia", "keyboard", "vision", "coordinate"
        self.confidence = confidence  # 0.0 - 1.0
        self.name = name
        self.type = type
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.action = action      # "invoke", "click", "focus", "type"
        self.error = error

    def __repr__(self):
        if self.found:
            return (f"ResolvedTarget({self.source}, '{self.name}', "
                    f"({self.x},{self.y}), conf={self.confidence:.2f})")
        return f"ResolvedTarget(not_found, error='{self.error}')"


def resolve_target(description, window=None, try_vision=False):
    """Resolve a UI target description through tiered strategies.

    Args:
        description: Natural language description of the target
                     (e.g. "Submit button", "search bar", "the OK button").
        window: Window to search in (None = active).
        try_vision: Whether to fall back to vision (slower, needs llava).

    Returns:
        ResolvedTarget with the best match found.
    """
    if not description:
        return ResolvedTarget(error="No target description provided")

    # --- Tier 1: UIA accessibility tree ---
    try:
        from automation.ui_control import find_control
        ctrl = find_control(name=description, window=window)
        if ctrl:
            return ResolvedTarget(
                found=True,
                source="uia",
                confidence=0.9,
                name=ctrl["name"],
                type=ctrl["type"],
                x=ctrl["x"], y=ctrl["y"],
                width=ctrl.get("width", 0),
                height=ctrl.get("height", 0),
                action="invoke" if ctrl.get("clickable") else "click",
            )
    except Exception as e:
        logger.debug(f"UIA resolve failed: {e}")

    # --- Tier 2: Role-based search ---
    # Parse description for role hints
    role_hints = {
        "button": "Button", "btn": "Button",
        "link": "Hyperlink", "hyperlink": "Hyperlink",
        "input": "Edit", "text field": "Edit", "text box": "Edit",
        "search bar": "Edit", "search field": "Edit",
        "checkbox": "CheckBox", "check box": "CheckBox",
        "tab": "TabItem", "menu": "MenuItem",
        "dropdown": "ComboBox", "combo": "ComboBox",
        "slider": "Slider",
    }

    desc_lower = description.lower()
    detected_role = None
    clean_name = description

    for hint, role in role_hints.items():
        if hint in desc_lower:
            detected_role = role
            # Remove role word from name for better matching
            clean_name = desc_lower.replace(hint, "").strip()
            break

    if detected_role and clean_name:
        try:
            from automation.ui_control import find_control
            ctrl = find_control(name=clean_name, role=detected_role,
                                window=window)
            if ctrl:
                return ResolvedTarget(
                    found=True,
                    source="uia",
                    confidence=0.85,
                    name=ctrl["name"],
                    type=ctrl["type"],
                    x=ctrl["x"], y=ctrl["y"],
                    width=ctrl.get("width", 0),
                    height=ctrl.get("height", 0),
                    action="invoke" if ctrl.get("clickable") else "click",
                )
        except Exception as e:
            logger.debug(f"Role-based resolve failed: {e}")

    # --- Tier 3: Vision fallback ---
    if try_vision:
        try:
            from vision import find_element
            result = find_element(description)
            if result.get("found"):
                return ResolvedTarget(
                    found=True,
                    source="vision",
                    confidence=0.6,
                    name=description,
                    x=result.get("x", 0),
                    y=result.get("y", 0),
                    action="click",
                )
        except Exception as e:
            logger.debug(f"Vision resolve failed: {e}")

    # Not found through any tier
    return ResolvedTarget(
        error=f"'{description}' not found via UIA or vision"
    )
