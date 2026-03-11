"""
LLM conversation context management.

Extracted from: brain.py::Brain context methods

Responsibility:
  - Maintain messages list (user/assistant/tool turns)
  - Context window trimming (keep last N messages)
  - Context collapsing (summarize old tool turns into single message)
  - Topic-aware sizing (same-topic: 12 messages, different: 6)
  - Ambient context injection (active window, clipboard, time-of-day)
  - Clean message preparation for API calls
  - Idle detection and auto-reset
"""

import re
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages LLM conversation context, topic tracking, and ambient context."""

    # Topic keyword map for topic extraction
    _TOPIC_KEYWORDS = {
        "weather": ["weather", "temperature", "rain", "forecast", "hot", "cold", "wind"],
        "news": ["news", "headlines", "current events", "happening"],
        "music": ["music", "song", "play", "spotify", "playlist"],
        "reminders": ["reminder", "remind", "alarm", "schedule"],
        "apps": ["open", "close", "minimize", "launch", "start"],
        "email": ["email", "mail", "send", "message"],
        "search": ["search", "google", "look up", "find"],
        "clipboard": ["clipboard", "copied", "paste", "what did i copy"],
        "calendar": ["calendar", "schedule", "meeting", "event", "appointment"],
    }

    # Triggers for clipboard/context injection
    # "this"/"that" are weak triggers (200 char limit); clipboard-explicit are strong (1000 char limit)
    _CONTEXT_TRIGGERS = re.compile(
        r'\b(this|that|clipboard|summarize this|send this|copy this|paste|'
        r'what did i copy|what\'s in my clipboard|what is in my clipboard|'
        r'my clipboard|translate this|translate that|translate clipboard|'
        r'summarize my clipboard|analyze this|analyze my clipboard|'
        r'explain this|explain my clipboard|read my clipboard|'
        r'what i copied|the text i copied|the code i copied|'
        r'what\'s on my clipboard|what is on my clipboard|'
        r'rewrite this|fix this|improve this|format this|'
        r'check this|review this|correct this|proofread this|'
        r'this link|this url|this page|this screenshot|this image|'
        r'check this link|open this link|read this link|read this page|'
        r'what\'s this link|what is this link|look at this|'
        r'the link i copied|the url i copied|what\'s in this image|'
        r'i copied a link|i copied a url|i copied an image|'
        r'i shared|i pasted|analyze this image|describe this image)\b', re.I)

    # Explicit clipboard patterns get a larger content limit (1000 chars)
    _CLIPBOARD_EXPLICIT = re.compile(
        r'\b(clipboard|what did i copy|what i copied|my clipboard|'
        r'summarize my clipboard|analyze my clipboard|explain my clipboard|'
        r'read my clipboard|translate clipboard|what\'s in my clipboard|'
        r'what is in my clipboard|what\'s on my clipboard|what is on my clipboard|'
        r'the text i copied|the code i copied|'
        r'this link|this url|this page|the link i copied|the url i copied|'
        r'i copied a link|i copied a url|read this link|read this page|'
        r'this screenshot|this image|analyze this image|describe this image|'
        r'i copied an image|what\'s in this image)\b', re.I)

    # URL pattern for detecting links in clipboard
    _URL_PATTERN = re.compile(
        r'https?://[^\s<>"\']+|www\.[^\s<>"\']+', re.I)

    def __init__(self, max_context=6):
        self.messages = []
        self.max_context = max_context

        # Topic tracking
        self._current_topic = None
        self._topic_turn_count = 0
        self._topic_last_time = 0

        # Idle detection
        self._last_think_time = 0

    @property
    def current_topic(self):
        return self._current_topic

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def append(self, message):
        """Append a message to the conversation."""
        self.messages.append(message)

    def trim(self):
        """Trim context by dropping oldest messages when limit exceeded.

        Always starts on a user message to avoid orphaned assistant messages.
        """
        if len(self.messages) <= self.max_context:
            return
        old_count = len(self.messages) - self.max_context
        self.messages = self.messages[old_count:]
        while self.messages and self.messages[0].get("role") != "user":
            self.messages.pop(0)
        logger.info(f"Context trimmed: dropped oldest messages, {len(self.messages)} remain")

    def reset(self, last_action_summary=""):
        """Clear conversation history but keep topic and optional action summary.

        Args:
            last_action_summary: Summary of last action for pronoun resolution
                                 (e.g. "[Last action: open_app({...}) -> Opened Chrome]").
        """
        preserved_topic = self._current_topic

        self.messages = []

        # Soft reset: keep topic for carryover
        self._current_topic = preserved_topic
        self._topic_turn_count = 0

        # Inject last action as ghost message for pronoun resolution
        if last_action_summary:
            self.messages.append({"role": "system", "content": last_action_summary})

        logger.info("Context reset (idle timeout or explicit)")

    def check_idle_reset(self, idle_threshold=120):
        """Check if idle too long. Updates the last-think timestamp.

        Returns True if the caller should reset context (idle > threshold).
        Does NOT auto-reset — the caller decides how to reset (may need to
        clear brain-specific flags too).
        """
        now = time.time()
        should_reset = (
            self._last_think_time > 0
            and (now - self._last_think_time) > idle_threshold
        )
        if should_reset:
            logger.info(f"Idle {now - self._last_think_time:.0f}s — caller should reset context")
        self._last_think_time = now
        return should_reset

    def collapse_completed_turn(self, final_response):
        """Collapse completed tool call/result messages into a clean summary.

        After each think() call, condenses the messages from this turn
        into a single opaque user->assistant pair. This prevents the LLM
        from seeing old tool call patterns and re-calling them (tool
        stickiness bug). The system prompt already teaches tool format.
        """
        if not final_response:
            return

        # Find the last user message (start of current turn)
        last_user_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return

        # Smart collapse: keep a 1-line semantic summary instead of destroying
        # all context. This preserves multi-turn coherence (follow-up questions,
        # "do that again", topic continuity) while preventing tool stickiness.
        user_msg = self.messages[last_user_idx].get("content", "")
        # Build a short summary: "User asked X → result" (max ~80 chars)
        user_short = user_msg[:60].replace("\n", " ").strip()
        resp_short = str(final_response)[:60].replace("\n", " ").strip()
        summary = f"[{user_short} → {resp_short}]"

        self.messages = self.messages[:last_user_idx]
        self.messages.append({"role": "user", "content": user_short})
        self.messages.append({"role": "assistant", "content": summary})

    def get_clean_messages(self, skip_tools=False):
        """Get messages suitable for API calls, condensing old tool context.

        Old tool call/result pairs are converted to plain assistant text
        summaries to prevent the LLM from copying previous tool calls.
        Only the CURRENT turn keeps raw tool messages (for multi-round
        tool calling). Previous turns are all condensed.

        Args:
            skip_tools: If True (prompt mode), strip all tool role messages.
        """
        # Find the start indices of each user turn
        turn_starts = []
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "user":
                turn_starts.append(i)

        recent_start = turn_starts[-1] if turn_starts else 0

        clean = []
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            role = msg.get("role")

            # Recent turns: keep everything raw
            if i >= recent_start:
                if skip_tools and role == "tool":
                    i += 1
                    continue
                if skip_tools and role == "assistant" and msg.get("tool_calls"):
                    content = msg.get("content") or "I'll take care of that."
                    clean.append({"role": "assistant", "content": content})
                    i += 1
                    continue
                if role in ("user", "assistant", "tool"):
                    clean.append(msg)
                i += 1
                continue

            # Old turns: condense assistant+tool sequences into result-only summary
            # Do NOT include tool names — they bias the LLM to reuse the same tool
            if role == "assistant" and msg.get("tool_calls"):
                results = []
                j = i + 1
                while j < len(self.messages) and self.messages[j].get("role") == "tool":
                    results.append(str(self.messages[j].get("content", ""))[:100])
                    j += 1
                summary = "; ".join(results) if results else "Done."
                if msg.get("content"):
                    summary = msg["content"] + " " + summary
                clean.append({"role": "assistant", "content": summary})
                i = j  # Skip past the tool result messages
                continue

            if role in ("user", "assistant"):
                clean.append(msg)
            # Skip orphaned tool messages from old turns
            i += 1

        return clean

    def pop_last_user_message(self):
        """Remove the last user message (on error, before it gets into context)."""
        if self.messages and self.messages[-1].get("role") == "user":
            self.messages.pop()

    # ------------------------------------------------------------------
    # Topic tracking
    # ------------------------------------------------------------------

    def extract_topic(self, text):
        """Extract topic from user text using keyword matching."""
        lower = text.lower()
        for topic, keywords in self._TOPIC_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return topic
        return None

    def update_topic(self, user_input):
        """Update topic tracking and adjust context window size.

        Returns the current topic after update.
        """
        new_topic = self.extract_topic(user_input)
        now = time.time()

        # Topic timeout: 120s idle resets topic
        if self._topic_last_time and (now - self._topic_last_time) > 120:
            self._current_topic = None
            self._topic_turn_count = 0

        if new_topic:
            if new_topic == self._current_topic:
                self._topic_turn_count += 1
                # Same topic -> increase context window (keep more history)
                self.max_context = min(6 + self._topic_turn_count * 2, 12)
            else:
                self._current_topic = new_topic
                self._topic_turn_count = 0
                self.max_context = 6  # Reset to default
        self._topic_last_time = now

        return self._current_topic

    # ------------------------------------------------------------------
    # Ambient context injection
    # ------------------------------------------------------------------

    def get_ambient_context(self, user_input):
        """Build ambient context string for system prompt injection.

        Includes: current topic, time of day, active window, clipboard (on trigger).
        """
        parts = []

        # Current conversation topic
        if self._current_topic:
            parts.append(f"Current topic: {self._current_topic}")

        # Time-of-day period
        hour = datetime.now().hour
        if hour < 12:
            parts.append("Time: morning")
        elif hour < 17:
            parts.append("Time: afternoon")
        elif hour < 21:
            parts.append("Time: evening")
        else:
            parts.append("Time: night")

        # Active window title
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if active and active.title:
                parts.append(f"Active window: {active.title[:80]}")
                if "spotify" in active.title.lower():
                    parts.append("Active app: Spotify (music player — interpret music-related requests as songs)")
        except Exception:
            pass

        # Clipboard content (only when user says "this"/"that"/"clipboard"/etc.)
        if self._CONTEXT_TRIGGERS.search(user_input):
            # Check for image in clipboard first
            clipboard_image = self._check_clipboard_image(user_input)
            if clipboard_image:
                parts.append(clipboard_image)
            else:
                # Text clipboard
                try:
                    import pyperclip
                    clip = pyperclip.paste()
                    if clip and len(clip.strip()) > 0:
                        explicit = bool(self._CLIPBOARD_EXPLICIT.search(user_input))
                        limit = 1000 if explicit else 300
                        clip_text = clip[:limit]
                        if len(clip) > limit:
                            clip_text += f"... ({len(clip)} chars total, truncated)"
                        # Detect URLs in clipboard and annotate
                        urls = self._URL_PATTERN.findall(clip)
                        if urls:
                            parts.append(f"Clipboard contains URL: {urls[0]}")
                            if len(urls) > 1:
                                parts.append(f"({len(urls)} URLs total)")
                            # Hint the LLM to use web_read for URLs
                            parts.append(
                                "HINT: Use web_read tool to fetch this URL if the user wants to read/summarize it")
                        else:
                            parts.append(f"Clipboard content: {clip_text}")
                except Exception:
                    pass

        return " | ".join(parts) if parts else ""

    def _check_clipboard_image(self, user_input):
        """Check if clipboard contains an image. Returns context string or None."""
        # Only check for image when user explicitly references screenshot/image
        image_triggers = re.compile(
            r'\b(screenshot|image|picture|photo|this image|this screenshot|'
            r'analyze this image|describe this image|what\'s in this image|'
            r'i copied an image|look at this)\b', re.I)
        if not image_triggers.search(user_input):
            return None
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img is not None:
                # Save to temp file for vision analysis
                import tempfile
                import os
                tmp = os.path.join(tempfile.gettempdir(), "g_clipboard_image.png")
                img.save(tmp)
                return (f"Clipboard contains an IMAGE saved to {tmp}. "
                        "HINT: Use take_screenshot tool or tell user what you see. "
                        "The image is from the user's clipboard, not a live screenshot.")
        except Exception:
            pass
        return None
