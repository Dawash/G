"""
Step and goal verification for desktop agent.

Extracted from desktop_agent.py: _verify_step(), _check_goal_done(),
_parse_result().

Responsibility:
  - Verify individual step completion via multi-layer checks
  - Verify overall goal completion from action history
  - Parse tool results into structured outcomes
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


class StepVerifier:
    """Multi-layer verification for agent steps and goals."""

    def verify_step(self, step_description, tool_result, screen_state):
        """Verify step completion using multi-layer verification.

        Priority order (fast -> slow):
          1. Tool result keywords (instant)
          2. Window title check (fast OS call)
          3. File existence check (for create_file steps)
          4. Process check (for open_app steps)
          5. Web extraction (for browser steps)
          6. Negative check (error indicators)
          7. Vision fallback (slow)

        Returns dict with verified (bool), details (str), web_content (str).
        """
        verified = False
        details = str(tool_result)
        web_content = ""
        result_lower = str(tool_result).lower()
        step_lower = step_description.lower()

        # Layer 1: Tool result keywords
        success_words = [
            "opened", "launched", "typed", "searched", "focused",
            "clicked", "scrolled", "pressed", "created file", "playing",
            "toggled",
        ]
        if any(w in result_lower for w in success_words):
            verified = True
            details += " | result keywords confirm success"

        # Layer 2: Window title check
        if not verified or "open" in step_lower or "launch" in step_lower:
            try:
                import pygetwindow as gw
                active = gw.getActiveWindow()
                if active and active.title:
                    title = active.title.lower()
                    skip = {"the", "my", "a", "an", "open", "launch"}
                    for keyword in step_lower.split():
                        if keyword in title and keyword not in skip:
                            verified = True
                            details += f" | window title '{active.title}' matches"
                            break
            except Exception:
                pass

        # Layer 3: File existence check
        if "create" in step_lower or "file" in step_lower or "document" in step_lower:
            path_match = re.search(r'[A-Z]:\\[^\s"\']+', str(tool_result))
            if path_match:
                filepath = path_match.group()
                if os.path.exists(filepath):
                    verified = True
                    details += f" | file exists: {filepath}"
                else:
                    details += f" | file NOT found: {filepath}"

        # Layer 4: Process/window check
        if not verified and ("open" in step_lower or "play" in step_lower):
            windows = screen_state.get("windows", [])
            skip = {"the", "my", "a", "an", "open", "play", "launch", "some"}
            for keyword in step_lower.split():
                if keyword in skip:
                    continue
                for w_title in windows:
                    if keyword.lower() in w_title.lower():
                        verified = True
                        details += f" | process/window found: {w_title}"
                        break
                if verified:
                    break

        # Layer 5: Web extraction
        if (not verified and
                ("browser" in step_lower or "web" in step_lower
                 or "search" in step_lower)):
            from automation.observer import ScreenObserver
            url = ScreenObserver.get_browser_url()
            if url:
                try:
                    from web_agent import web_read
                    web_content = web_read(url)[:500]
                    if web_content and len(web_content) > 50:
                        verified = True
                        details += (f" | Web content available "
                                    f"({len(web_content)} chars)")
                except Exception as e:
                    logger.debug(f"Web extraction failed: {e}")

        # Layer 6: Negative check — error indicators override
        error_indicators = [
            "error", "failed", "not found", "denied", "crash",
            "stopped working", "not responding", "blocked",
        ]
        if any(ind in result_lower for ind in error_indicators):
            verified = False
            details += " | Negative check: error keywords in tool result"

        # Layer 7: Vision fallback
        if not verified and screen_state:
            summary = screen_state.get("summary", "").lower()
            if any(ind in summary for ind in error_indicators):
                verified = False
                details += f" | Screen shows error: {summary[:100]}"
            elif screen_state.get("foreground", ""):
                if not any(ind in result_lower for ind in error_indicators):
                    verified = True
                    details += " | Foreground visible, no errors detected"

        return {"verified": verified, "details": details,
                "web_content": web_content}

    @staticmethod
    def check_goal_done(goal, history):
        """Check if all parts of goal are accomplished from tool results.

        Returns a summary string if done, None otherwise.
        """
        if not history:
            return None

        goal_lower = goal.lower()
        tools_used = [
            (h.get("tool", ""), h.get("result", ""), h.get("args", {}))
            for h in history
        ]

        # Parse what the goal needs
        needs = {
            "open": any(w in goal_lower for w in ["open", "launch", "start"]),
            "type": any(w in goal_lower for w in ["type", "write", "enter text"]),
            "search": any(w in goal_lower for w in ["search", "find", "look up"]),
            "close": any(w in goal_lower for w in ["close", "quit", "exit"]),
            "play": any(w in goal_lower for w in ["play", "listen", "music", "song"]),
            "sysinfo": any(w in goal_lower for w in [
                "disk", "ram", "cpu", "memory", "process", "ip", "network",
                "system info"]),
            "files": any(w in goal_lower for w in [
                "move file", "copy file", "delete file", "zip", "organize"]),
            "install": any(w in goal_lower for w in [
                "install", "uninstall", "update"]),
        }

        # Check what was accomplished
        did = {
            "open": any(
                any(w in r.lower() for w in ["opened", "launched", "focused", "opening"])
                for t, r, a in tools_used if t in ("open_app", "focus_window")),
            "type": any(
                "typed" in r.lower() and "characters" in r.lower()
                for t, r, a in tools_used if t == "type_text"),
            "search": any(
                "search" in r.lower()
                for t, r, a in tools_used
                if t in ("search_in_app", "google_search")),
            "close": any(
                "closed" in r.lower() or "not found" not in r.lower()
                for t, r, a in tools_used if t == "close_app"),
            "terminal": any(
                t == "run_terminal" and "error" not in r.lower()
                for t, r, a in tools_used),
            "files": any(
                t == "manage_files" and "error" not in r.lower()
                for t, r, a in tools_used),
            "software": any(
                t == "manage_software" and "error" not in r.lower()
                for t, r, a in tools_used),
        }

        # Play requires search in music app + press_key
        did["play"] = (
            did["search"]
            and any("spotify" in str(a).lower() or "music" in str(a).lower()
                    for t, r, a in tools_used if t == "search_in_app")
            and any(t == "press_key" for t, r, a in tools_used)
        )
        if not did["play"]:
            did["play"] = any(t == "play_music" for t, r, a in tools_used)

        # Check completeness
        all_done = True
        parts = []

        checks = [
            ("sysinfo", "terminal", "got system info"),
            ("files", "files", "managed files"),
            ("install", "software", "managed software"),
        ]
        for need_key, did_key, label in checks:
            if needs[need_key]:
                if did[did_key]:
                    parts.append(label)
                else:
                    all_done = False

        if needs["open"]:
            if did["open"] or did["search"]:
                parts.append("opened app")
            else:
                all_done = False
        if needs["type"]:
            if did["type"]:
                parts.append("typed text")
            else:
                all_done = False
        if needs["search"]:
            if did["search"]:
                parts.append("searched")
            else:
                all_done = False
        if needs["play"]:
            if did["play"]:
                parts.append("playing music")
            else:
                all_done = False
        if needs["close"]:
            if did["close"]:
                parts.append("closed app")
            else:
                all_done = False

        if all_done and parts:
            return f"Done! {', '.join(parts).capitalize()} for: {goal}"
        return None

    @staticmethod
    def parse_result(tool_name, args, raw_result):
        """Parse tool result string into structured outcome.

        Returns dict with status, evidence, next_hint, raw.
        """
        result_str = str(raw_result).lower()
        outcome = {
            "status": "unknown",
            "evidence": str(raw_result)[:200],
            "next_hint": "",
            "raw": raw_result,
        }

        success_words = [
            "opened", "launched", "created", "typed", "searched",
            "clicked", "scrolled", "pressed", "playing", "toggled",
            "focused", "closed",
        ]
        if any(w in result_str for w in success_words):
            outcome["status"] = "success"

        fail_words = [
            "error", "not found", "failed", "couldn't", "timeout",
            "blocked", "denied", "not installed",
        ]
        if any(w in result_str for w in fail_words):
            outcome["status"] = "fail"
            if "not found" in result_str:
                outcome["next_hint"] = ("App not found. Try search_in_app "
                                        "or check installed apps.")
            elif "timeout" in result_str:
                outcome["next_hint"] = "Action timed out. Try simpler approach."
            elif "error" in result_str:
                outcome["next_hint"] = "Try alternative tool from escalation map."

        if any(w in result_str for w in ["but", "however", "partially"]):
            outcome["status"] = "partial"
            outcome["next_hint"] = ("Partially done — verify what's missing "
                                    "and complete.")

        if outcome["status"] == "unknown":
            outcome["status"] = "success"

        return outcome
