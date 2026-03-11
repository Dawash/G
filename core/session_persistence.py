"""
Session continuity — auto-persist and restore session state across restarts.

When G shuts down (gracefully, crash, or periodic auto-save), this module
saves conversation context, topic, and session metadata to session_state.json.
On next startup, it restores everything so the user picks up where they left off.

Saves:
  - Last 20 conversation messages (from brain.messages)
  - Current topic (from brain._ctx._current_topic)
  - Tool blacklist (from brain._tool_blacklist)
  - Last user input, last response, last_mode_was_agent (from SessionState)
  - Timestamp for freshness check

Restore rules:
  - Only restores if session_state.json exists AND is less than 24 hours old
  - Corrupted / missing files are handled gracefully (never crashes)

File safety:
  - Atomic writes: write to .tmp file, then os.replace() to final path
  - All public methods are wrapped in try/except
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Session state file lives in the project root (next to config.json)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION_FILE = os.path.join(_PROJECT_ROOT, "session_state.json")
_SESSION_TMP = _SESSION_FILE + ".tmp"

# Sessions older than 24 hours are stale — start fresh
_MAX_AGE_SECONDS = 24 * 60 * 60


class SessionPersistence:
    """Persist and restore session state across assistant restarts."""

    def __init__(self, path=None):
        self._path = path or _SESSION_FILE
        self._tmp_path = self._path + ".tmp"

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, brain, session_state):
        """Save current session state to disk.

        Args:
            brain: Brain instance (has export_session() method).
            session_state: SessionState instance (from core.state).

        Returns:
            bool: True if saved successfully, False otherwise.
        """
        try:
            # Export brain state
            brain_data = brain.export_session()

            # Export session state
            state_data = {
                "timestamp": time.time(),
                "brain": brain_data,
                "session": {
                    "last_response": getattr(session_state, "last_response", None),
                    "last_user_input": getattr(session_state, "last_user_input", None),
                    "last_mode_was_agent": getattr(session_state, "last_mode_was_agent", False),
                    "mode": getattr(session_state, "mode", "ACTIVE"),
                },
            }

            # Atomic write: write to .tmp, then replace
            with open(self._tmp_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())

            os.replace(self._tmp_path, self._path)
            logger.debug("Session state saved to %s", self._path)
            return True

        except Exception as e:
            logger.warning("Failed to save session state: %s", e)
            # Clean up temp file if it exists
            try:
                if os.path.exists(self._tmp_path):
                    os.remove(self._tmp_path)
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore(self, brain, session_state):
        """Restore session state from disk into live objects.

        Args:
            brain: Brain instance (has import_session() method).
            session_state: SessionState instance (from core.state).

        Returns:
            bool: True if restored successfully, False if no valid session found.
        """
        try:
            if not os.path.isfile(self._path):
                logger.debug("No session file found at %s", self._path)
                return False

            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Freshness check: reject sessions older than 24 hours
            saved_time = data.get("timestamp", 0)
            age = time.time() - saved_time
            if age > _MAX_AGE_SECONDS:
                logger.info(
                    "Session file is %.1f hours old (max 24h) — starting fresh",
                    age / 3600,
                )
                self.clear()
                return False

            # Restore brain state
            brain_data = data.get("brain", {})
            if brain_data:
                brain.import_session(brain_data)

            # Restore session state
            session_data = data.get("session", {})
            if session_data:
                last_response = session_data.get("last_response")
                if last_response is not None:
                    session_state.last_response = last_response

                last_user_input = session_data.get("last_user_input")
                if last_user_input is not None:
                    session_state.last_user_input = last_user_input

                last_mode_was_agent = session_data.get("last_mode_was_agent")
                if last_mode_was_agent is not None:
                    session_state.last_mode_was_agent = last_mode_was_agent

            logger.info(
                "Session restored (age=%.0fm, messages=%d)",
                age / 60,
                len(brain_data.get("messages", [])),
            )
            return True

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Session file is corrupted, starting fresh: %s", e)
            self.clear()
            return False
        except Exception as e:
            logger.warning("Failed to restore session state: %s", e)
            return False

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    def clear(self):
        """Delete the session state file.

        Returns:
            bool: True if cleared (or already absent), False on error.
        """
        try:
            if os.path.isfile(self._path):
                os.remove(self._path)
                logger.debug("Session file cleared: %s", self._path)
            # Also clean up any leftover temp file
            if os.path.isfile(self._tmp_path):
                os.remove(self._tmp_path)
            return True
        except Exception as e:
            logger.warning("Failed to clear session file: %s", e)
            return False
