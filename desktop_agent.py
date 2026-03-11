"""
Desktop Agent — truly autonomous agentic executor with full screen awareness.

Architecture: OBSERVE -> THINK -> ACT -> OBSERVE -> ADAPT (continuous loop)

Unlike a blind script runner, this agent:
  1. Looks at the screen BEFORE and AFTER every action
  2. Understands what's happening (which app is focused, blockers, errors)
  3. Diagnoses problems (wrong window, popup blocking, action failed)
  4. Finds workarounds autonomously (dismiss popup, switch window, alt approach)
  5. Re-plans dynamically when the original plan isn't working
  6. Can spawn sub-agents for parallel subtasks

The LLM acts as the BRAIN at every decision point — not just for planning,
but for real-time "what do I see? what went wrong? what should I do?"

NOTE: This is the active implementation. A decomposed version exists in
automation/ with the same logic split across focused modules:
  - automation/desktop_agent.py  (DesktopAgentV2 — orchestrator)
  - automation/planner.py        (AgentPlanner — plan generation/replanning)
  - automation/observer.py       (ScreenObserver — screenshots/vision/windows)
  - automation/verifier.py       (StepVerifier — step/goal verification)
  - automation/recovery.py       (FailureRecovery — diagnosis/self-healing)
See docs/agent-policy.md for the escalation policy and execution budgets.
"""

import json
import logging
import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

def _log_agent_action(action, result, success=True):
    """Log action to shared action log (brain module)."""
    try:
        from brain import log_action
        log_action("desktop_agent", action, str(result)[:200], success)
    except Exception:
        pass

# ===================================================================
# Configuration
# ===================================================================

MAX_AGENT_TURNS = 15          # Max observe-think-act cycles per task
MAX_RECOVERY_ATTEMPTS = 2     # Max tries to fix a single problem
AFTER_ACTION_WAIT = 1.0       # Seconds to wait for UI to settle after action
def _get_ollama_url():
    """Get the Ollama URL from config, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_URL
        cfg = load_config()
        return cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    except Exception:
        return "http://localhost:11434"

OLLAMA_API = _get_ollama_url()
_STATE_FILE = os.path.join(os.path.dirname(__file__), "agent_state.json")
_TOOL_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "tool_memory.json")

# 3-Phase execution constants
PHASE_RECON = "RECON"
PHASE_EXECUTE = "EXECUTE"
PHASE_VERIFY = "VERIFY"
MAX_RECON_TURNS = 3           # Max turns for reconnaissance phase
MAX_EXECUTE_TURNS = 12        # Max turns for execution phase
CHECKPOINT_INTERVAL = 3       # Checkpoint every N actions
MAX_BACKTRACK_ATTEMPTS = 2    # Max replans before giving up
TAKEOVER_TIMEOUT = 120        # Seconds to wait for user on sensitive screens

# Takeover patterns — agent pauses for user on these screens
_TAKEOVER_PATTERNS = [
    (r"(?:enter|type).{0,20}password|(?:sign|log).?in.{0,20}(?:account|email|user)|windows security|credential", "login_screen",
     "I see a login screen. Please sign in and say continue when ready."),
    (r"payment|checkout|credit.?card", "payment_screen",
     "I see a payment screen. Please handle this and say continue."),
    (r"captcha|verify.?you.?are.?human", "captcha",
     "There's a CAPTCHA. Please solve it and say continue."),
    (r"verification.?code|two.?factor|2fa", "two_factor",
     "Two-factor authentication needed. Please enter the code and say continue."),
    (r"user.?account.?control|administrator", "permission",
     "A permission dialog appeared. Please handle it and say continue."),
]

# Pre-action safety guardrails (minimal — user has full access)
_BLOCKED_COMMANDS = set()  # No blocked commands — full system access
_SENSITIVE_DOMAINS = set()  # No domain restrictions

# Recovery hints for common failures (base set + dynamically learned)
_RECOVERY_HINTS = {
    "not found": "Try opening the app first with open_app, then retry",
    "out of bounds": "Use search_in_app instead of click_at for more reliable targeting",
    "not installed": "Use google_search to find a web alternative",
    "timed out": "Wait longer or try a simpler approach",
    "access denied": "Skip this step — insufficient permissions",
}
_LEARNED_HINTS_FILE = os.path.join(os.path.dirname(__file__), "learned_hints.json")

# Tools the agent can use — includes brain tools for direct execution
AVAILABLE_TOOLS = [
    "open_app", "close_app", "click_at", "type_text",
    "press_key", "scroll", "search_in_app", "google_search",
    "focus_window", "run_command",
    # Brain tools — agent can call these directly (faster than UI interaction)
    "run_terminal", "manage_files", "manage_software",
    "get_weather", "get_time", "get_news", "set_reminder",
    "create_file", "web_read", "toggle_setting", "play_music",
    # Precision interaction tools (Phase 16: prefer UIA-based)
    "click_control", "click_element", "find_on_screen",
    "inspect_window", "set_control_text",
    "snap_window", "list_windows",
    "manage_tabs", "fill_form",
    # Browser automation (Phase 17)
    "browser_action",
]

# Tool escalation paths: if tool A fails, try tool B
_TOOL_ALTERNATIVES = {
    "open_app": ["search_in_app", "run_command"],
    "click_at": ["click_control", "search_in_app", "press_key"],
    "click_control": ["click_element", "click_at", "press_key"],
    "search_in_app": ["google_search", "type_text"],
    "focus_window": ["open_app", "click_at"],
    "toggle_setting": ["run_terminal", "run_command"],
    "type_text": ["press_key"],
    "run_command": ["run_terminal"],
    "run_terminal": ["run_command"],
    "manage_software": ["run_terminal"],
    "manage_files": ["run_terminal"],
    "browser_action": ["click_control", "google_search"],
    "manage_tabs": ["browser_action", "press_key"],
}


# ===================================================================
# Desktop Agent — Observe/Think/Act loop
# ===================================================================

class DesktopAgent:
    """
    Truly autonomous agent that sees the screen, thinks about what to do,
    acts, then observes the result and adapts. Not a blind script runner.

    Optionally uses OpenClaw (https://openclaw.ai) for advanced system
    control, browser automation, and messaging when installed.
    """

    _active_instance = None  # Track running agent for emergency stop

    def __init__(self, action_registry, reminder_mgr=None,
                 ollama_model=None, speak_fn=None):
        self.action_registry = action_registry
        self.reminder_mgr = reminder_mgr
        if ollama_model is None:
            try:
                from config import load_config, DEFAULT_OLLAMA_MODEL
                ollama_model = load_config().get("ollama_model", DEFAULT_OLLAMA_MODEL)
            except Exception:
                ollama_model = "qwen2.5:7b"
        self.ollama_model = ollama_model
        self.speak_fn = speak_fn
        self._history = []         # Full action history for context
        self._turn_count = 0
        self._plan_steps = []      # Pre-planned steps from _plan()
        self._stuck_count = 0      # How many times stuck detected consecutively
        self._current_goal = ""    # Current goal for progress summary
        self._current_plan_idx = 0 # Current plan step index
        self._cancelled = False    # Set by cancel() to stop the loop
        self._phase = None         # Current phase: RECON / EXECUTE / VERIFY
        self._checkpoint_state = None  # Last checkpoint for backtracking
        self._backtrack_count = 0  # Number of backtracks used
        self._actions_since_checkpoint = 0  # Actions since last checkpoint
        self._success_criteria = ""  # Extracted from plan output

        # Load learned recovery hints from previous sessions
        DesktopAgent.load_learned_hints()

        # Check for OpenClaw (optional advanced tools)
        self._openclaw_tools = {}
        try:
            from openclaw_bridge import is_openclaw_installed, get_openclaw_tools
            if is_openclaw_installed():
                self._openclaw_tools = get_openclaw_tools()
                logger.info(f"OpenClaw active: {len(self._openclaw_tools)} extra tools available")
        except ImportError:
            pass

    def cancel(self):
        """Signal the agent to stop at the next loop iteration."""
        self._cancelled = True
        logger.info("DesktopAgent: cancel requested")

    def _build_progress_summary(self):
        """Build goal + progress + failures block for context injection (attention recitation)."""
        summary = f"ORIGINAL GOAL: {self._current_goal}\n"
        if self._plan_steps:
            summary += f"FULL PLAN: {' → '.join(self._plan_steps)}\n"
            summary += f"CURRENT STEP: {self._current_plan_idx + 1}/{len(self._plan_steps)}\n"

        if self._history:
            summary += "RECENT ACTIONS:\n"
            for h in self._history[-5:]:
                result_lower = h.get("result", "").lower()
                status = "FAILED" if "error" in result_lower or "not found" in result_lower else "OK"
                summary += f"  [{status}] {h.get('tool', '?')}({json.dumps(h.get('args', {}))}) → {h.get('result', '')[:80]}\n"

        # Error preservation — failed approaches the model must NOT repeat
        failures = [h for h in self._history
                    if "error" in h.get("result", "").lower() or "not found" in h.get("result", "").lower()
                    or "blocked" in h.get("result", "").lower()]
        if failures:
            summary += "FAILED APPROACHES (do NOT repeat these):\n"
            for f in failures[-3:]:
                summary += f"  - {f.get('tool', '?')} with {json.dumps(f.get('args', {}))} failed: {f.get('result', '')[:100]}\n"

        return summary

    def _save_state(self):
        """Persist agent state to disk for crash recovery."""
        state = {
            "goal": self._current_goal,
            "plan": self._plan_steps,
            "completed_steps": [i for i, h in enumerate(self._history) if h.get("plan_step") and "error" not in h.get("result", "").lower()],
            "current_step": self._current_plan_idx,
            "timestamp": time.time(),
        }
        try:
            with open(_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_state(self, goal):
        """Check for resumable state matching this goal."""
        try:
            if not os.path.exists(_STATE_FILE):
                return None
            with open(_STATE_FILE) as f:
                state = json.load(f)
            # Only resume if same goal and less than 5 min old
            if state.get("goal") == goal and time.time() - state.get("timestamp", 0) < 300:
                return state
        except Exception:
            pass
        return None

    def _clear_state(self):
        """Remove persisted agent state on completion."""
        try:
            os.remove(_STATE_FILE)
        except OSError:
            pass

    def _record_tool_outcome(self, tool_name, args, success):
        """Track tool success/failure rates for learning across sessions."""
        key = f"{tool_name}:{json.dumps(args, sort_keys=True)[:100]}"
        try:
            memory = {}
            if os.path.exists(_TOOL_MEMORY_FILE):
                with open(_TOOL_MEMORY_FILE) as f:
                    memory = json.load(f)
            entry = memory.get(key, {"success": 0, "fail": 0})
            if success:
                entry["success"] = entry.get("success", 0) + 1
            else:
                entry["fail"] = entry.get("fail", 0) + 1
                entry["last_error"] = str(self._history[-1].get("result", ""))[:200] if self._history else ""
            entry["last_used"] = time.time()
            memory[key] = entry
            # Prune old entries (keep last 200)
            if len(memory) > 200:
                sorted_keys = sorted(memory, key=lambda k: memory[k].get("last_used", 0))
                for old_key in sorted_keys[:len(memory) - 200]:
                    del memory[old_key]
            with open(_TOOL_MEMORY_FILE, "w") as f:
                json.dump(memory, f, indent=2)
        except Exception as e:
            logger.debug(f"Tool memory write failed: {e}")

    def _get_tool_memory(self, tool_name, args=None):
        """Check if a tool+args combo has a known success/failure pattern."""
        try:
            if not os.path.exists(_TOOL_MEMORY_FILE):
                return None
            with open(_TOOL_MEMORY_FILE) as f:
                memory = json.load(f)
            if args:
                key = f"{tool_name}:{json.dumps(args, sort_keys=True)[:100]}"
                return memory.get(key)
            # Return aggregate for tool name
            total_success = 0
            total_fail = 0
            for k, v in memory.items():
                if k.startswith(f"{tool_name}:"):
                    total_success += v.get("success", 0)
                    total_fail += v.get("fail", 0)
            if total_success + total_fail > 0:
                return {"success": total_success, "fail": total_fail,
                        "rate": total_success / (total_success + total_fail)}
            return None
        except Exception:
            return None

    def execute(self, goal):
        """
        Main entry point. Autonomously accomplish a desktop goal.

        3-Phase architecture (inspired by OpenAI CUA):
          Phase 1 — RECON: Observe screen, detect blockers, check app state
          Phase 2 — EXECUTE: Plan-guided think→act→verify with checkpointing
          Phase 3 — VERIFY: Confirm overall goal, generate report
        """
        from vision import ensure_vision_model

        available, msg = ensure_vision_model()
        if not available:
            logger.warning(f"Vision not available: {msg}")
            return f"I can't see the screen yet. {msg}"

        DesktopAgent._active_instance = self
        self._history = []
        self._turn_count = 0
        self._plan_steps = []
        self._current_goal = goal
        self._current_plan_idx = 0
        self._backtrack_count = 0
        self._actions_since_checkpoint = 0
        self._checkpoint_state = None

        # Check for resumable state from a previous crash
        resumed = self._load_state(goal)
        if resumed:
            self._plan_steps = resumed.get("plan", [])
            self._current_plan_idx = resumed.get("current_step", 0)
            logger.info(f"Resumed agent state: step {self._current_plan_idx}/{len(self._plan_steps)}")

        # Log goal start
        _log_agent_action("start_goal", goal)

        try:
            # Minimize our own terminal so it doesn't confuse vision
            self._minimize_own_terminal()

            # Check if this is a multi-part goal we should split into sub-agents
            subtasks = self._detect_parallel_subtasks(goal)
            if subtasks and len(subtasks) > 1:
                return self._execute_parallel(subtasks, goal)

            # ===== PHASE 1: RECON =====
            self._phase = PHASE_RECON
            recon = self._phase_recon(goal)
            if recon.get("abort"):
                return recon["message"]

            # Plan (skip if resumed)
            if not self._plan_steps:
                print("Thinking...")
                self._plan_steps = self._plan(goal)
                if self._plan_steps:
                    logger.info(f"Plan ({len(self._plan_steps)} steps): {self._plan_steps}")
                else:
                    logger.info("No plan generated, using reactive mode")
            else:
                print(f"Resuming from step {self._current_plan_idx + 1}/{len(self._plan_steps)}...")

            # ===== PHASE 2: EXECUTE =====
            self._phase = PHASE_EXECUTE
            exec_result = self._phase_execute(goal)

            # ===== PHASE 3: VERIFY =====
            self._phase = PHASE_VERIFY
            return self._phase_verify(goal, exec_result)
        finally:
            DesktopAgent._active_instance = None

    # ==================================================================
    # PHASE 1: RECON — observe screen, detect blockers, check state
    # ==================================================================

    def _phase_recon(self, goal):
        """Reconnaissance phase: observe without acting (max 3 turns).

        Returns dict with keys:
          abort (bool): True if we should abort
          message (str): Abort reason
          obstacles (list): Detected obstacles
          available_apps (list): Currently visible/running apps
        """
        logger.info(f"RECON phase for: {goal[:60]}")
        obstacles = []
        available_apps = []
        recon_start = time.time()

        for turn in range(MAX_RECON_TURNS):
            if time.time() - recon_start > 30:  # 30s max for recon
                logger.warning("RECON phase timeout (30s)")
                break
            if self._cancelled:
                return {"abort": True, "message": "Cancelled."}

            # OS-level app inventory (fast, no LLM)
            visible_windows = self._get_window_inventory()
            running_apps = self._get_running_apps()
            available_apps = visible_windows

            # Check for takeover-worthy screens (login, payment, etc.)
            screen_text = " ".join(visible_windows).lower()
            takeover = self._check_takeover(screen_text)
            if takeover:
                # Pause for user
                waited = self._wait_for_user_takeover(takeover)
                if not waited:
                    return {"abort": True,
                            "message": f"Stopped — {takeover['type']} screen detected and user didn't continue."}
                continue  # Re-observe after user handled it

            # Check for simple blockers (popups, dialogs) with smart dismissal
            try:
                import pygetwindow as gw
                active = gw.getActiveWindow()
                if active and active.title:
                    title_lower = active.title.lower()
                    # Categorize blocker type for targeted dismissal
                    blocker_type = None
                    if any(kw in title_lower for kw in ["cookie", "accept", "consent"]):
                        blocker_type = "cookie"
                    elif any(kw in title_lower for kw in ["profile", "choose profile", "select profile"]):
                        blocker_type = "profile"
                    elif any(kw in title_lower for kw in ["default browser", "set as default"]):
                        blocker_type = "default"
                    elif any(kw in title_lower for kw in ["allow", "permission", "notification"]):
                        blocker_type = "permission"
                    elif any(kw in title_lower for kw in ["select", "choose"]):
                        blocker_type = "chooser"

                    if blocker_type:
                        obstacles.append(f"Blocker ({blocker_type}): {active.title}")
                        try:
                            import pyautogui
                            if blocker_type == "cookie":
                                # Cookie banners: try Tab+Enter to hit Accept/OK button
                                pyautogui.press("tab")
                                time.sleep(0.2)
                                pyautogui.press("enter")
                                time.sleep(0.5)
                                logger.info(f"RECON: dismissed cookie banner with Tab+Enter")
                            elif blocker_type == "default":
                                # Default browser: usually has "Not now" or close
                                pyautogui.hotkey("alt", "F4")
                                time.sleep(0.5)
                                logger.info(f"RECON: dismissed default browser dialog with Alt+F4")
                            elif blocker_type == "permission":
                                # Notification permission: dismiss with Escape
                                pyautogui.press("escape")
                                time.sleep(0.5)
                                logger.info(f"RECON: dismissed permission dialog with Escape")
                            else:
                                # Profile picker / chooser: try Escape first, then Enter
                                pyautogui.press("escape")
                                time.sleep(0.3)
                                # Check if still there
                                still_active = gw.getActiveWindow()
                                if still_active and still_active.title == active.title:
                                    pyautogui.press("enter")  # Select default option
                                    time.sleep(0.5)
                                    logger.info(f"RECON: dismissed chooser with Enter (default)")
                                else:
                                    logger.info(f"RECON: dismissed blocker with Escape")
                        except Exception:
                            pass
                        continue  # Re-observe
            except Exception:
                pass

            # No blockers found — recon complete
            break

        logger.info(f"RECON complete: {len(obstacles)} obstacles, {len(available_apps)} apps visible")
        return {
            "abort": False,
            "message": "",
            "obstacles": obstacles,
            "available_apps": available_apps,
        }

    # ==================================================================
    # PHASE 3: VERIFY — confirm overall goal completion
    # ==================================================================

    def _phase_verify(self, goal, exec_result):
        """Final verification phase: confirm goal, generate report, cleanup."""
        # If execute phase returned a definitive result, use it
        if exec_result and ("Completed" in str(exec_result) or "Done" in str(exec_result)):
            self._clear_state()
            return exec_result

        # Check goal completion from action history
        auto_done = self._check_goal_done(goal)
        if auto_done:
            self._clear_state()
            return auto_done

        # If we have a progress report (ran out of turns), return it
        if exec_result:
            self._clear_state()
            self.self_heal()  # Learn from failures
            return exec_result

        # Fallback
        self._clear_state()
        self.self_heal()
        return self._build_progress_report(goal)

    # ==================================================================
    # PHASE 2: EXECUTE — plan-guided loop with checkpointing + backtrack
    # ==================================================================

    def _phase_execute(self, goal):
        """Execute phase with checkpointing every 3 actions and backtracking.

        Returns the final result string (or progress report if ran out of turns).
        """
        return self._agentic_loop(goal)

    def _agentic_loop(self, goal):
        """
        The heart of the agent. Plan-first, then execute with verification:
          1. Use pre-planned steps to guide execution (if available)
          2. OBSERVE the screen
          3. THINK about the current planned step (or react to screen)
          4. ACT (execute the decided action)
          5. VERIFY step completion (vision + web extraction)
          6. DIAGNOSE failures and retry
          7. Checkpoint every 3 actions, backtrack when stuck
          8. Detect takeover screens (login, payment, CAPTCHA)
        """
        for turn in range(MAX_EXECUTE_TURNS):
            if self._cancelled:
                logger.info("Agent cancelled by timeout — stopping")
                return "Task was cancelled (took too long)."

            self._turn_count = turn + 1
            current_step = None
            if self._plan_steps and self._current_plan_idx < len(self._plan_steps):
                current_step = self._plan_steps[self._current_plan_idx]
            logger.info(f"=== Agent turn {turn+1}/{MAX_EXECUTE_TURNS} for: {goal} "
                        f"(plan step: {current_step or 'reactive'}) ===")

            # --- OBSERVE ---
            try:
                screen_state = self._observe(goal)
            except Exception as e:
                logger.error(f"Observation failed: {e}")
                screen_state = {"summary": f"Error observing screen: {e}", "blocked": False}
            logger.info(f"Observation: {screen_state.get('summary', 'no summary')[:200]}")

            # --- TAKEOVER CHECK (login, payment, CAPTCHA, 2FA) ---
            screen_text = screen_state.get("summary", "").lower()
            takeover = self._check_takeover(screen_text)
            if takeover:
                waited = self._wait_for_user_takeover(takeover)
                if not waited:
                    return f"Stopped — {takeover['type']} screen detected."
                continue  # Re-observe after user handled it

            # --- STUCK DETECTION + BACKTRACKING ---
            if self._is_stuck():
                self._stuck_count += 1
                last_tool = self._history[-1].get("tool", "") if self._history else ""
                logger.warning(f"Agent is stuck (repeating {last_tool}). Stuck count: {self._stuck_count}")

                # Try backtracking before giving up (replan remaining steps)
                if self._stuck_count >= 2 and self._backtrack_count < MAX_BACKTRACK_ATTEMPTS:
                    logger.info(f"Backtracking (attempt {self._backtrack_count + 1}/{MAX_BACKTRACK_ATTEMPTS})")
                    old_remaining = tuple(self._plan_steps[self._current_plan_idx:])
                    if self._backtrack(goal, screen_state):
                        new_remaining = tuple(self._plan_steps[self._current_plan_idx:])
                        if old_remaining == new_remaining:
                            logger.warning("Backtrack produced identical plan — skipping")
                        else:
                            self._stuck_count = 0
                            self._actions_since_checkpoint = 0
                            continue  # Retry with new plan

                # Force completion after being stuck too many times
                if self._stuck_count >= 3:
                    done_msg = f"I attempted '{goal}' but kept getting stuck. Some steps may have completed."
                    self._speak(done_msg)
                    return done_msg
            else:
                self._stuck_count = 0

            if self._is_stuck():
                last_tool = self._history[-1].get("tool", "") if self._history else ""
                alt_advice = "Try a COMPLETELY DIFFERENT tool."
                if last_tool == "focus_window":
                    alt_advice = (
                        "focus_window keeps failing! Instead:\n"
                        "- Use search_in_app which opens AND searches directly\n"
                        "- Or use open_app to re-open the app\n"
                        "- Or just proceed with the next step anyway"
                    )
                elif last_tool == "open_app":
                    alt_advice = (
                        "open_app keeps failing! Instead:\n"
                        "- Try search_in_app if you need to search\n"
                        "- Or try google_search as a fallback\n"
                        "- Or say DONE if the app actually did open"
                    )
                elif last_tool == "press_key":
                    alt_advice = (
                        "Pressing keys isn't working! Stop pressing keys. Instead:\n"
                        "- Use a different tool (open_app, search_in_app)\n"
                        "- Or say DONE if the goal was already achieved"
                    )
                elif last_tool == "search_in_app":
                    alt_advice = (
                        "search_in_app keeps repeating! The search already worked.\n"
                        "- Say DONE — the search was successful, music is playing.\n"
                        "- Do NOT search again. The task is COMPLETE."
                    )
                screen_state["stuck_warning"] = (
                    f"WARNING: {last_tool} has FAILED repeatedly. "
                    f"It is NOT working. {alt_advice}"
                )

            # --- AUTO-COMPLETION CHECK ---
            if self._history:
                last = self._history[-1]
                last_result = last.get("result", "")
                completed_parts = []
                if "opened" in last_result.lower() or "launched" in last_result.lower():
                    completed_parts.append(f"App opened: {last_result}")
                if "typed" in last_result.lower() and "characters" in last_result.lower():
                    completed_parts.append(f"Text typed successfully: {last_result}")
                if "searching" in last_result.lower() or "searched" in last_result.lower():
                    completed_parts.append(f"Search done: {last_result}")
                if completed_parts:
                    screen_state["completed_actions"] = "; ".join(completed_parts)

            # --- DIRECT TOOL SHORTCUT: skip vision for steps with clear tool mapping ---
            direct_decision = self._try_direct_tool(current_step) if current_step else None
            direct_result = None
            if direct_decision:
                decision = direct_decision
                logger.info(f"Direct tool shortcut: {decision.get('tool')}({decision.get('args', {})})")
            else:
                # --- THINK (plan-guided or reactive) ---
                if current_step:
                    screen_state["current_plan_step"] = current_step
                    remaining = self._plan_steps[self._current_plan_idx:]
                    screen_state["remaining_plan"] = remaining[:3]  # Show next 3 steps
                decision = self._think(goal, screen_state)
            logger.info(f"Decision: {decision.get('action', 'none')} — {decision.get('reasoning', '')[:200]}")

            # CLI-first: try PowerShell before vision/LLM
            if current_step and not direct_result:
                cli_result = self._try_cli_first(current_step)
                if cli_result:
                    self._history.append({
                        "turn": self._turn_count,
                        "saw": "skipped (CLI)",
                        "decided": f"CLI: {current_step[:40]}",
                        "tool": "run_terminal",
                        "args": {},
                        "result": str(cli_result)[:200],
                        "parsed_status": "success",
                        "next_hint": "",
                        "plan_step": current_step,
                    })
                    self._current_plan_idx += 1
                    self._turn_count += 1
                    continue

            action = decision.get("action", "")

            # Done?
            if action == "DONE":
                summary = decision.get("summary", f"Completed: {goal}")
                self._clear_state()
                self._speak(summary)
                return summary

            # Give up?
            if action == "GIVE_UP":
                reason = decision.get("reasoning", "Could not complete the task")
                self._clear_state()
                self._speak(f"I couldn't complete the task. {reason}")
                return f"Could not complete: {goal}. {reason}"

            # --- SAFETY CHECK ---
            tool_name = decision.get("tool", "")
            safe, reason = self._safety_check(decision)
            if not safe:
                logger.warning(f"Safety blocked: {reason}")
                self._history.append({
                    "turn": turn + 1, "saw": screen_state.get("summary", ""),
                    "decided": f"BLOCKED: {reason}", "tool": tool_name,
                    "args": decision.get("args", {}), "result": f"BLOCKED: {reason}",
                    "plan_step": current_step,
                })
                continue

            # --- ACT ---
            self._speak_action(decision)
            self._announce_progress(decision)

            # Handle run_command specially
            if tool_name == "run_command":
                cmd = decision.get("args", {}).get("command", "")
                result = self._run_terminal_command(cmd)
            else:
                result = self._act(decision)
            logger.info(f"Action result: {str(result)[:200]}")

            # Parse result into structured outcome
            parsed = self._parse_result(tool_name, decision.get("args", {}), result)
            logger.info(f"Parsed: status={parsed['status']}, hint={parsed['next_hint'][:80]}")

            # Record to history (with structured info)
            self._history.append({
                "turn": turn + 1,
                "saw": screen_state.get("summary", ""),
                "decided": f"{action}: {decision.get('reasoning', '')}",
                "tool": tool_name,
                "args": decision.get("args", {}),
                "result": str(result)[:300],
                "parsed_status": parsed["status"],
                "next_hint": parsed["next_hint"],
                "plan_step": current_step,
            })

            _log_agent_action(tool_name or action, result,
                              "error" not in str(result).lower())

            # --- CHECKPOINT (every 3 actions) ---
            self._actions_since_checkpoint += 1
            if self._actions_since_checkpoint >= CHECKPOINT_INTERVAL:
                self._checkpoint(goal, screen_state)
                self._actions_since_checkpoint = 0

            # --- VERIFY step ---
            if current_step:
                verification = self._verify_step(current_step, result, screen_state)
                if verification["verified"]:
                    logger.info(f"Step verified: {current_step}")
                    self._current_plan_idx += 1
                    self._save_state()
                    # Record success for tool memory
                    self._record_tool_outcome(tool_name, decision.get("args", {}), True)
                else:
                    # --- ESCALATION: diagnose → retry → alternative → move on ---
                    logger.warning(f"Step NOT verified: {current_step} — {verification['details']}")
                    self._record_tool_outcome(tool_name, decision.get("args", {}), False)
                    step_fixed = False

                    # Level 1: LLM diagnosis with different args
                    fix = self._diagnose(current_step, verification["details"], screen_state)
                    if fix.get("fix_action"):
                        logger.info(f"Escalation L1: {fix.get('diagnosis')} → {fix['fix_action']}")
                        fix_result = self._act({
                            "action": "USE_TOOL", "tool": fix["fix_action"],
                            "args": fix.get("fix_args", {}),
                            "reasoning": fix.get("diagnosis", "L1 fix"),
                        })
                        self._history.append({
                            "turn": turn + 1, "saw": "fix L1",
                            "decided": f"FIX-L1: {fix.get('diagnosis', '')}",
                            "tool": fix["fix_action"], "args": fix.get("fix_args", {}),
                            "result": str(fix_result)[:300], "plan_step": f"FIX: {current_step}",
                        })
                        fix_screen = self._observe(goal, use_vision=True)
                        fix_verify = self._verify_step(current_step, fix_result, fix_screen)
                        if fix_verify["verified"]:
                            step_fixed = True
                            self._record_tool_outcome(fix["fix_action"], fix.get("fix_args", {}), True)

                    # Level 2: Try alternative tool from escalation map
                    if not step_fixed and tool_name in _TOOL_ALTERNATIVES:
                        for alt_tool in _TOOL_ALTERNATIVES[tool_name]:
                            alt_args = self._build_alt_args(alt_tool, decision.get("args", {}), current_step)
                            logger.info(f"Escalation L2: trying {alt_tool} instead of {tool_name}")
                            alt_result = self._act({
                                "action": "USE_TOOL", "tool": alt_tool,
                                "args": alt_args, "reasoning": f"Alternative for failed {tool_name}",
                            })
                            self._history.append({
                                "turn": turn + 1, "saw": "fix L2",
                                "decided": f"FIX-L2: {alt_tool} as alt for {tool_name}",
                                "tool": alt_tool, "args": alt_args,
                                "result": str(alt_result)[:300], "plan_step": f"ALT: {current_step}",
                            })
                            alt_screen = self._observe(goal, use_vision=True)
                            alt_verify = self._verify_step(current_step, alt_result, alt_screen)
                            if alt_verify["verified"]:
                                step_fixed = True
                                self._record_tool_outcome(alt_tool, alt_args, True)
                                break
                            self._record_tool_outcome(alt_tool, alt_args, False)

                    if step_fixed:
                        self._current_plan_idx += 1
                        self._save_state()
                    else:
                        # No fix worked — move on to avoid infinite loop
                        logger.warning(f"All escalation levels failed for step: {current_step}")
                        self._current_plan_idx += 1

            # --- QUICK GOAL COMPLETION CHECK ---
            auto_done = self._check_goal_done(goal)
            if auto_done:
                self._clear_state()
                self._speak(auto_done)
                return auto_done

            # Wait for UI
            time.sleep(AFTER_ACTION_WAIT)

        # Ran out of turns — report partial progress and self-heal
        self._clear_state()
        self.self_heal()  # Learn from this session's failures
        return self._build_progress_report(goal)

    # ==================================================================
    # Checkpointing, Backtracking, Takeover
    # ==================================================================

    def _checkpoint(self, goal, screen_state):
        """Save agent state for backtrack recovery. No LLM call — just raw state."""
        self._checkpoint_state = {
            "plan_steps": list(self._plan_steps),
            "current_step_idx": self._current_plan_idx,
            "screen_foreground": screen_state.get("foreground", ""),
            "history_len": len(self._history),
            "timestamp": time.time(),
        }
        self._save_state()
        logger.info(f"Checkpoint saved at step {self._current_plan_idx}/{len(self._plan_steps)}")

    def _backtrack(self, goal, screen_state):
        """Replan remaining steps from current state. Uses 1 LLM call.

        Returns True if replan succeeded, False otherwise.
        """
        self._backtrack_count += 1
        completed_steps = self._plan_steps[:self._current_plan_idx]
        failed_step = self._plan_steps[self._current_plan_idx] if self._current_plan_idx < len(self._plan_steps) else "unknown"

        # Collect failure context
        recent_failures = [h for h in self._history[-5:]
                          if "error" in h.get("result", "").lower()
                          or "not found" in h.get("result", "").lower()
                          or "blocked" in h.get("result", "").lower()]
        failure_context = ""
        if recent_failures:
            failure_context = "Failed approaches:\n"
            for f in recent_failures[-3:]:
                failure_context += f"  - {f.get('tool', '?')}: {f.get('result', '')[:80]}\n"

        prompt = (
            f"A desktop automation plan got stuck. Replan the REMAINING steps.\n\n"
            f"ORIGINAL GOAL: {goal}\n"
            f"COMPLETED STEPS: {', '.join(completed_steps) if completed_steps else 'none'}\n"
            f"STUCK ON: {failed_step}\n"
            f"CURRENT SCREEN: {screen_state.get('summary', 'unknown')[:200]}\n"
            f"{failure_context}\n"
            f"Give a NEW numbered list of remaining steps (max 8). Use DIFFERENT approaches.\n"
            f"Available tools: {', '.join(AVAILABLE_TOOLS)}\n"
            f"Respond ONLY as a numbered list."
        )

        for attempt in range(2):  # Retry once on timeout
            try:
                resp = requests.post(
                    f"{OLLAMA_API}/api/chat",
                    json={
                        "model": self.ollama_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.4, "num_predict": 300},
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                lines = re.findall(r'^\s*\d+[\.\)]\s*(.+)', content, re.MULTILINE)
                if lines:
                    new_steps = [line.strip().strip('"\'') for line in lines[:8] if line.strip()]
                    if new_steps:
                        # Replace remaining plan steps
                        self._plan_steps = completed_steps + new_steps
                        self._current_plan_idx = len(completed_steps)
                        self._save_state()
                        logger.info(f"Backtrack: replanned {len(new_steps)} new steps: {new_steps}")
                        self._think_log(f"Replanning: {len(new_steps)} new steps")
                        return True
                break  # Got a response but no valid steps — don't retry
            except requests.exceptions.Timeout:
                logger.warning(f"Backtrack replan timeout (attempt {attempt + 1}/2)")
                if attempt == 0:
                    continue  # Retry once
            except Exception as e:
                logger.warning(f"Backtrack replan failed: {e}")
                break  # Non-timeout error — don't retry

        return False

    def _check_takeover(self, screen_text):
        """Check if current screen requires user takeover (login, payment, etc.).

        Args:
            screen_text: Lowercased screen observation text

        Returns:
            dict with {type, message} if takeover needed, None otherwise.
        """
        for pattern, takeover_type, message in _TAKEOVER_PATTERNS:
            if re.search(pattern, screen_text, re.IGNORECASE):
                logger.info(f"Takeover detected: {takeover_type}")
                return {"type": takeover_type, "message": message}
        return None

    def _wait_for_user_takeover(self, takeover):
        """Pause agent and wait for user to handle sensitive screen.

        Speaks the message and listens for "continue" or "stop".
        Returns True if user said continue, False if timeout/stop.
        """
        self._speak(takeover["message"])
        logger.info(f"Waiting for user takeover: {takeover['type']}")

        start = time.time()
        while time.time() - start < TAKEOVER_TIMEOUT:
            if self._cancelled:
                return False
            try:
                from speech import listen
                response = listen()
                if response:
                    response_lower = response.lower().strip()
                    if any(w in response_lower for w in ["continue", "done", "ready", "go ahead", "proceed"]):
                        logger.info(f"User completed takeover: {takeover['type']}")
                        return True
                    if any(w in response_lower for w in ["stop", "cancel", "abort", "quit"]):
                        logger.info(f"User cancelled at takeover: {takeover['type']}")
                        return False
            except Exception as e:
                logger.debug(f"Listen during takeover failed: {e}")
                time.sleep(2)

        logger.warning(f"Takeover timeout ({TAKEOVER_TIMEOUT}s): {takeover['type']}")
        self._speak("I've been waiting too long. Stopping the task.")
        return False

    def self_heal(self):
        """Read error log and tool memory to learn new recovery hints.

        Enhanced with:
        - Skill saving: stores successful sequences as reusable skills
        - Reflexion: stores failure reflections for future attempts

        Call this after a failed test round or periodically.
        Identifies top failure patterns and adds workarounds.
        """
        # --- SKILL SAVING: save successful sequences ---
        self._save_successful_skill()

        # --- REFLEXION: store failure reflections ---
        self._store_failure_reflections()

        try:
            # Load existing learned hints
            learned = {}
            if os.path.exists(_LEARNED_HINTS_FILE):
                with open(_LEARNED_HINTS_FILE) as f:
                    learned = json.load(f)

            # Analyze tool memory for high-failure tools
            if os.path.exists(_TOOL_MEMORY_FILE):
                with open(_TOOL_MEMORY_FILE) as f:
                    memory = json.load(f)

                # Aggregate failures by tool
                tool_fails = {}
                for key, data in memory.items():
                    tool = key.split(":")[0]
                    if data.get("fail", 0) > data.get("success", 0):
                        if tool not in tool_fails:
                            tool_fails[tool] = {"count": 0, "errors": []}
                        tool_fails[tool]["count"] += data["fail"]
                        if data.get("last_error"):
                            tool_fails[tool]["errors"].append(data["last_error"][:100])

                # Generate hints for frequently failing tools
                for tool, info in tool_fails.items():
                    if info["count"] >= 3 and tool not in learned:
                        # Find the most common error pattern
                        errors = info["errors"]
                        if errors:
                            common_error = max(set(errors), key=errors.count)
                            hint = f"{tool} frequently fails. Common error: {common_error[:50]}. Try alternative tools."
                            if tool in _TOOL_ALTERNATIVES:
                                alts = _TOOL_ALTERNATIVES[tool]
                                hint += f" Alternatives: {', '.join(alts)}"
                            learned[tool] = hint
                            logger.info(f"Self-heal: learned hint for {tool}: {hint[:80]}")

            # Analyze assistant.log for recurring errors
            log_file = os.path.join(os.path.dirname(__file__), "assistant.log")
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    # Read last 500 lines
                    lines = f.readlines()[-500:]
                error_counts = {}
                for line in lines:
                    if "ERROR" in line or "error" in line.lower():
                        # Extract error category
                        for pattern in ["timeout", "connection", "not found", "permission",
                                         "json", "parse", "ollama"]:
                            if pattern in line.lower():
                                error_counts[pattern] = error_counts.get(pattern, 0) + 1
                                break

                for error_type, count in error_counts.items():
                    if count >= 5 and error_type not in learned:
                        learned[error_type] = f"Recurring '{error_type}' errors ({count}x). Add extra error handling."
                        logger.info(f"Self-heal: detected recurring {error_type} ({count}x)")

            # Persist learned hints
            if learned:
                with open(_LEARNED_HINTS_FILE, "w") as f:
                    json.dump(learned, f, indent=2)

                # Merge into runtime recovery hints
                for key, hint in learned.items():
                    if key not in _RECOVERY_HINTS:
                        _RECOVERY_HINTS[key] = hint

            return f"Self-heal complete. {len(learned)} hints learned."
        except Exception as e:
            logger.warning(f"Self-heal failed: {e}")
            return f"Self-heal error: {e}"

    def _save_successful_skill(self):
        """Save successful tool sequences as reusable skills (Voyager pattern).

        After a successful task execution, stores the tool sequence
        so it can be replayed for similar future tasks.
        """
        if not self._history or not self._current_goal:
            return

        # Check if we had a mostly successful run
        successes = [h for h in self._history
                     if "error" not in h.get("result", "").lower()
                     and "not found" not in h.get("result", "").lower()
                     and h.get("tool")]
        failures = [h for h in self._history
                    if ("error" in h.get("result", "").lower()
                        or "not found" in h.get("result", "").lower())
                    and h.get("tool")]

        # Only save if more successes than failures
        if len(successes) < 2 or len(failures) >= len(successes):
            return

        try:
            from skills import SkillLibrary
            skill_lib = SkillLibrary()

            # Build tool sequence from successful history
            tool_sequence = []
            for h in self._history:
                if not h.get("tool"):
                    continue
                tool_sequence.append({
                    "tool": h["tool"],
                    "args": h.get("args", {}),
                    "description": h.get("plan_step", h.get("decided", "")),
                    "result": h.get("result", "")[:100],
                })

            if len(tool_sequence) < 2:
                return

            # Generate skill name and save
            name = skill_lib.generate_skill_name(self._current_goal)

            # Determine tags
            tags = []
            tools_used = {h["tool"] for h in tool_sequence}
            if tools_used & {"open_app", "click_at", "type_text", "press_key"}:
                tags.append("desktop")
            if tools_used & {"google_search", "web_read", "browser_action"}:
                tags.append("web")
            if tools_used & {"create_file", "manage_files"}:
                tags.append("files")
            if tools_used & {"run_terminal", "run_command", "manage_software"}:
                tags.append("system")

            skill_lib.save_skill(
                name=name,
                description=f"Automated: {self._current_goal[:100]}",
                goal=self._current_goal,
                tool_sequence=tool_sequence,
                tags=tags,
            )
            logger.info(f"Saved skill from successful execution: {name} ({len(tool_sequence)} steps)")

        except Exception as e:
            logger.debug(f"Failed to save skill: {e}")

    def _store_failure_reflections(self):
        """Store reflections from failed steps for future Reflexion retrieval."""
        if not self._history or not self._current_goal:
            return

        failures = [h for h in self._history
                    if ("error" in h.get("result", "").lower()
                        or "not found" in h.get("result", "").lower())
                    and h.get("tool")]

        if not failures:
            return

        try:
            from skills import SkillLibrary
            skill_lib = SkillLibrary()
            name = skill_lib.generate_skill_name(self._current_goal)

            # Generate a summary reflection
            failed_tools = [f"{h['tool']}({json.dumps(h.get('args', {}))[:40]})"
                           for h in failures[-3:]]
            reflection = (
                f"Goal '{self._current_goal[:50]}' had {len(failures)} failures. "
                f"Failed tools: {', '.join(failed_tools)}. "
                f"Consider alternative approaches for these tools."
            )
            skill_lib.add_reflection(name, reflection)
        except Exception as e:
            logger.debug(f"Failed to store failure reflections: {e}")

    @staticmethod
    def load_learned_hints():
        """Load learned recovery hints from disk into runtime memory."""
        try:
            if os.path.exists(_LEARNED_HINTS_FILE):
                with open(_LEARNED_HINTS_FILE) as f:
                    learned = json.load(f)
                for key, hint in learned.items():
                    if key not in _RECOVERY_HINTS:
                        _RECOVERY_HINTS[key] = hint
                logger.info(f"Loaded {len(learned)} learned recovery hints")
        except Exception:
            pass

    def _build_progress_report(self, goal):
        """Build a detailed progress report when the agent runs out of turns."""
        if not self._plan_steps:
            return f"I worked on '{goal}' for {MAX_AGENT_TURNS} steps but couldn't fully confirm completion."

        completed = []
        failed = []
        for i, step in enumerate(self._plan_steps):
            step_actions = [h for h in self._history if h.get("plan_step") == step]
            if any(h.get("parsed_status") == "success" for h in step_actions):
                completed.append(step)
            elif step_actions:
                failed.append(step)

        total = len(self._plan_steps)
        done_count = len(completed)
        report = f"Completed {done_count}/{total} steps for: {goal}"
        if completed:
            report += f"\n  Done: {', '.join(completed[:5])}"
        if failed:
            report += f"\n  Failed: {', '.join(failed[:3])}"
        remaining = [s for s in self._plan_steps if s not in completed and s not in failed]
        if remaining:
            report += f"\n  Not attempted: {', '.join(remaining[:3])}"
        return report

    def _get_memory_hints(self):
        """Generate hints from tool memory for the think prompt."""
        try:
            if not os.path.exists(_TOOL_MEMORY_FILE):
                return ""
            with open(_TOOL_MEMORY_FILE) as f:
                memory = json.load(f)
            hints = []
            # Find tools with high failure rates
            tool_stats = {}
            for k, v in memory.items():
                tool = k.split(":")[0]
                if tool not in tool_stats:
                    tool_stats[tool] = {"s": 0, "f": 0}
                tool_stats[tool]["s"] += v.get("success", 0)
                tool_stats[tool]["f"] += v.get("fail", 0)
            for tool, stats in tool_stats.items():
                total = stats["s"] + stats["f"]
                if total >= 3 and stats["f"] / total > 0.6:
                    hints.append(f"WARNING: {tool} has {stats['f']}/{total} failure rate — consider alternatives")
            if hints:
                return "TOOL RELIABILITY:\n" + "\n".join(f"- {h}" for h in hints[:3]) + "\n"
            return ""
        except Exception:
            return ""

    def _build_alt_args(self, alt_tool, original_args, step_description):
        """Build arguments for an alternative tool based on the original args and step."""
        name = original_args.get("name", "")
        query = original_args.get("query", step_description)
        if alt_tool == "search_in_app":
            return {"app": name or "Google", "query": query}
        elif alt_tool == "run_command":
            if name:
                return {"command": f"start {name}"}
            return {"command": f"echo {step_description}"}
        elif alt_tool == "open_app":
            return {"name": name or query.split()[0] if query else ""}
        elif alt_tool == "google_search":
            return {"query": query or name}
        elif alt_tool == "press_key":
            return {"keys": "enter"}
        elif alt_tool == "type_text":
            return {"text": query or name}
        elif alt_tool == "system_command":
            return {"command": query}
        return original_args

    def _safety_check(self, decision):
        """Pre-action safety guardrail. Returns (safe, reason)."""
        tool = decision.get("tool", "")
        args = decision.get("args", {})

        # Block destructive terminal commands
        if tool == "run_command":
            cmd = args.get("command", "").lower()
            for blocked in _BLOCKED_COMMANDS:
                if blocked in cmd:
                    return False, f"Blocked dangerous command: {cmd}"

        # Warn on sensitive domains in URLs or args
        args_lower = str(args).lower()
        for domain in _SENSITIVE_DOMAINS:
            if domain in args_lower:
                logger.warning(f"Safety: sensitive domain detected ({domain})")
                return False, f"Sensitive action involving {domain} — skipping for safety"

        # Block if same action failed 2+ times with same args
        if self._history:
            same_failures = [h for h in self._history[-4:]
                            if h.get("tool") == tool
                            and ("error" in h.get("result", "").lower()
                                 or "not found" in h.get("result", "").lower())]
            if len(same_failures) >= 2:
                return False, f"{tool} has failed {len(same_failures)} times — try different approach"

        return True, ""

    def _check_goal_done(self, goal):
        """
        Check if all parts of the goal have been accomplished based on
        tool results (not vision). This catches cases where llava can't
        read screen text but tools already reported success.
        """
        if not self._history:
            return None

        goal_lower = goal.lower()
        tools_used = [(h.get("tool", ""), h.get("result", ""), h.get("args", {}))
                      for h in self._history]

        # Parse what the goal needs
        needs_open = any(w in goal_lower for w in ["open", "launch", "start"])
        needs_type = any(w in goal_lower for w in ["type", "write", "enter text"])
        needs_search = any(w in goal_lower for w in ["search", "find", "look up"])
        needs_close = any(w in goal_lower for w in ["close", "quit", "exit"])
        needs_play = any(w in goal_lower for w in ["play", "listen", "music", "song"])

        # Check what was accomplished
        did_open = any("opened" in r.lower() or "launched" in r.lower() or "focused" in r.lower()
                       or "opening" in r.lower()
                       for t, r, a in tools_used if t in ("open_app", "focus_window"))
        # Terminal/file/software tasks count as done if tool returned successfully
        did_terminal = any(t == "run_terminal" and "error" not in r.lower()
                           for t, r, a in tools_used)
        did_files = any(t == "manage_files" and "error" not in r.lower()
                        for t, r, a in tools_used)
        did_software = any(t == "manage_software" and "error" not in r.lower()
                           for t, r, a in tools_used)
        did_type = any("typed" in r.lower() and "characters" in r.lower()
                       for t, r, a in tools_used if t == "type_text")
        did_search = any("search" in r.lower()
                         for t, r, a in tools_used if t in ("search_in_app", "google_search"))
        did_close = any("closed" in r.lower() or "not found" not in r.lower()
                        for t, r, a in tools_used if t == "close_app")
        # "Play" requires: search in a music app + press_key (enter/play) to actually start
        # Just searching is NOT enough — we need to confirm playback started
        did_play = did_search and any(
            "spotify" in str(a).lower() or "music" in str(a).lower()
            for t, r, a in tools_used if t == "search_in_app"
        ) and any(t == "press_key" for t, r, a in tools_used)
        # Also accept if we used the play_music tool directly
        if not did_play:
            did_play = any(t == "play_music" for t, r, a in tools_used)

        # System info / file / software tasks
        needs_sysinfo = any(w in goal_lower for w in ["disk", "ram", "cpu", "memory", "process", "ip", "network", "system info"])
        needs_files = any(w in goal_lower for w in ["move file", "copy file", "delete file", "zip", "organize"])
        needs_install = any(w in goal_lower for w in ["install", "uninstall", "update"])

        # Check if all needed parts are done
        all_done = True
        parts = []
        if needs_sysinfo:
            if did_terminal:
                parts.append("got system info")
            else:
                all_done = False
        if needs_files:
            if did_files:
                parts.append("managed files")
            else:
                all_done = False
        if needs_install:
            if did_software:
                parts.append("managed software")
            else:
                all_done = False
        if needs_open:
            # open_app OR search_in_app both open the app
            if did_open or did_search:
                parts.append("opened app")
            else:
                all_done = False
        if needs_type:
            if did_type:
                parts.append("typed text")
            else:
                all_done = False
        if needs_search:
            if did_search:
                parts.append("searched")
            else:
                all_done = False
        if needs_play:
            if did_play:
                parts.append("playing music")
            else:
                all_done = False
        if needs_close:
            if did_close:
                parts.append("closed app")
            else:
                all_done = False

        if all_done and parts:
            return f"Done! {', '.join(parts).capitalize()} for: {goal}"
        return None

    # ==================================================================
    # DIRECT TOOL SHORTCUTS — skip vision for steps with clear tool mapping
    # ==================================================================

    # Pattern → (tool, arg_extractor) for direct execution
    _DIRECT_STEP_PATTERNS = [
        # Open/launch app
        (re.compile(r"^open\s+(?:the\s+)?(.+?)(?:\s+app(?:lication)?)?$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "open_app", "args": {"name": m.group(1)}, "reasoning": "direct open"}),
        # Close app
        (re.compile(r"^close\s+(?:the\s+)?(.+?)$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "close_app", "args": {"name": m.group(1)}, "reasoning": "direct close"}),
        # Search in app
        (re.compile(r"^search\s+(?:for\s+)?[\"']?(.+?)[\"']?\s+(?:in|on)\s+(.+?)$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "search_in_app", "args": {"query": m.group(1), "app": m.group(2)}, "reasoning": "direct search"}),
        # Run terminal command
        (re.compile(r"^(?:run|execute|check)\s+(?:the\s+)?(?:command\s+)?[\"']?(.+?)[\"']?$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "run_terminal", "args": {"command": m.group(1)}, "reasoning": "direct terminal"}),
        # Install software
        (re.compile(r"^install\s+(.+?)(?:\s+using\s+winget)?$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "manage_software", "args": {"action": "install", "name": m.group(1)}, "reasoning": "direct install"}),
        # Get system info (disk, ram, etc.)
        (re.compile(r"^(?:check|get|show)\s+(?:the\s+)?(?:system\s+)?(?:disk|storage|ram|memory|cpu|battery|ip|network)", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "run_terminal", "args": {"command": "Get-ComputerInfo | Select-Object OsName,OsTotalVisibleMemorySize,CsProcessors"}, "reasoning": "direct sysinfo"}),
        # Create file
        (re.compile(r"^create\s+(?:a\s+)?(?:new\s+)?file\s+(?:called\s+)?[\"']?(.+?)[\"']?$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "create_file", "args": {"path": m.group(1), "content": ""}, "reasoning": "direct create"}),
        # Move/copy files
        (re.compile(r"^(move|copy)\s+(.+?)\s+to\s+(.+?)$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "manage_files", "args": {"action": m.group(1).lower(), "path": m.group(2), "destination": m.group(3)}, "reasoning": "direct file op"}),
        # Get weather
        (re.compile(r"^(?:check|get|show)\s+(?:the\s+)?weather", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "get_weather", "args": {}, "reasoning": "direct weather"}),
        # Toggle setting
        (re.compile(r"^(?:turn|toggle|enable|disable)\s+(on|off)\s+(.+?)$", re.I),
         lambda m: {"action": "USE_TOOL", "tool": "toggle_setting", "args": {"setting": m.group(2), "state": m.group(1)}, "reasoning": "direct toggle"}),
    ]

    def _try_direct_tool(self, step_description):
        """Try to match a plan step to a direct tool call, skipping vision/LLM.

        Returns a decision dict if matched, None otherwise.
        """
        if not step_description:
            return None
        step = step_description.strip()
        for pattern, builder in self._DIRECT_STEP_PATTERNS:
            m = pattern.match(step)
            if m:
                decision = builder(m)
                logger.info(f"Direct tool match: '{step}' → {decision['tool']}")
                return decision
        return None

    def _try_cli_first(self, step_description):
        """Try to execute a step via CLI (PowerShell) before vision-based approach.

        Returns result string if CLI handled it, None otherwise.
        """
        try:
            from execution_strategies import match_cli_command, execute_cli
            cmd = match_cli_command(step_description)
            if cmd:
                self._think_log(f"CLI shortcut: {cmd[:60]}")
                result = execute_cli(cmd)
                if result and "error" not in result.lower():
                    return result
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"CLI-first failed: {e}")
        return None

    # ==================================================================
    # PLAN — ask LLM for a step-by-step plan before executing
    # ==================================================================

    def _plan(self, goal):
        """Ask LLM for a step-by-step plan before executing."""
        prompt = (
            f"Plan how to accomplish this task on a Windows computer:\n"
            f"TASK: {goal}\n\n"
            f"Give a numbered list of steps (max 10). Each step should be ONE action.\n"
            f"Use simple descriptions, not code or function calls.\n"
            f"Available tools: {', '.join(AVAILABLE_TOOLS)}\n"
            f"CRITICAL RULES:\n"
            f"  1. NEVER use mouse clicks when a direct tool or terminal command works\n"
            f"  2. Priority: direct tool > run_terminal > keyboard shortcut > mouse click\n"
            f"  3. To create files: use create_file (NOT open editor and type)\n"
            f"  4. To open files: use run_terminal with 'start filename.html' (NOT click in explorer)\n"
            f"  5. To play music: use play_music (NOT navigate Spotify UI)\n"
            f"  6. To install apps: use manage_software (NOT download from browser)\n"
            f"  7. For system info: use run_terminal (NOT open Settings)\n"
            f"  8. Maximum 5 steps. Most tasks need 2-3 steps.\n"
            f"  9. Do NOT add monitoring, volume, shuffle, or cleanup steps\n"
            f"Common terminal recipes:\n"
            f"  - Open file in browser: run_terminal 'start filename.html'\n"
            f"  - Check disk space: run_terminal 'Get-PSDrive C'\n"
            f"  - List processes: run_terminal 'Get-Process | Sort CPU -Desc | Select -First 10'\n"
            f"  - Check IP: run_terminal 'ipconfig'\n"
            f"Example steps:\n"
            f'  1. Open the Firefox browser\n'
            f'  2. Search for "Python tutorials" in the browser\n'
            f'  3. Click on the first result\n\n'
            f"Last line: SUCCESS_CRITERIA: how to know the task is complete.\n"
            f"Respond ONLY as a numbered list (no JSON, no code)."
        )
        try:
            resp = requests.post(
                f"{OLLAMA_API}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 300},
                },
                timeout=20,
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            # Extract numbered list lines (most reliable format from LLM)
            lines = re.findall(r'^\s*\d+[\.\)]\s*(.+)', content, re.MULTILINE)
            if lines:
                # Clean up: remove trailing comments, function-call syntax
                cleaned = []
                for line in lines[:10]:
                    line = re.sub(r'\s*//.*$', '', line)  # Remove // comments
                    line = re.sub(r'\s*#.*$', '', line)   # Remove # comments
                    line = line.strip().strip('"\'')
                    if line:
                        cleaned.append(line)
                if cleaned:
                    # Validate plan: remove vague/monitoring/cleanup steps
                    _INVALID_STEP_PATTERNS = [
                        r"check.*(status|occasionally|periodically)",
                        r"adjust.*(volume|settings|preferences)",
                        r"set.*(shuffle|repeat|mode)",
                        r"minimize.*(window|player|app)",
                        r"close.*(player|app|when done)",
                        r"verify.*(functionality|working|result)",
                        r"wait for",
                    ]
                    validated = []
                    for step in cleaned:
                        step_lower = step.lower()
                        if any(re.search(p, step_lower) for p in _INVALID_STEP_PATTERNS):
                            logger.info(f"Plan validation: removed vague step '{step}'")
                            continue
                        validated.append(step)
                    # Cap at 5 steps max
                    validated = validated[:5]
                    if validated:
                        # Annotate each step with preferred execution strategy
                        try:
                            from execution_strategies import get_selector
                            selector = get_selector()
                            for i, step in enumerate(validated):
                                strategies = selector.select_strategies(step)
                                if strategies and strategies[0][0] != "vision":
                                    best = strategies[0][0]
                                    logger.debug(f"Plan step {i+1} preferred strategy: {best}")
                        except Exception:
                            pass
                        return validated
                    return cleaned[:5]  # Fallback: return original capped at 5
            # Extract success criteria if present
            criteria_match = re.search(r'SUCCESS_CRITERIA:\s*(.+)', content, re.IGNORECASE)
            if criteria_match:
                self._success_criteria = criteria_match.group(1).strip()
                logger.info(f"Success criteria: {self._success_criteria}")

            # Fallback: try JSON extraction
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                # Strip comments and fix trailing commas before parsing
                raw = json_match.group()
                raw = re.sub(r'//[^\n]*', '', raw)
                raw = re.sub(r',\s*([}\]])', r'\1', raw)  # Fix trailing commas
                data = json.loads(raw)
                steps = data.get("steps", [])
                if isinstance(steps, list) and steps:
                    return [str(s).strip() for s in steps[:10]]
        except Exception as e:
            logger.warning(f"Planning failed: {e}")
        return []

    # ==================================================================
    # VERIFY — check if a step succeeded using vision + web extraction
    # ==================================================================

    def _verify_step(self, step_description, tool_result, screen_state):
        """Verify step completion using multi-layer verification.

        Priority order (fast → slow):
          1. Tool result keywords (instant)
          2. Window title check (fast OS call, reliable)
          3. File existence check (for create_file steps)
          4. Process check (for open_app steps)
          5. Web extraction (for browser steps)
          6. Vision (slow, only if all else fails)
        """
        verified = False
        details = str(tool_result)
        web_content = ""
        result_lower = str(tool_result).lower()
        step_lower = step_description.lower()

        # --- Layer 1: Tool result keywords (instant) ---
        if any(w in result_lower for w in ["opened", "launched", "typed", "searched",
                                             "focused", "clicked", "scrolled", "pressed",
                                             "created file", "playing", "toggled"]):
            verified = True
            details += " | result keywords confirm success"

        # --- Layer 2: Window title check (fast, reliable for app tasks) ---
        if not verified or "open" in step_lower or "launch" in step_lower:
            try:
                import pygetwindow as gw
                active = gw.getActiveWindow()
                if active and active.title:
                    title = active.title.lower()
                    # Extract app name from step description
                    for keyword in step_lower.split():
                        if keyword in title and keyword not in ("the", "my", "a", "an", "open", "launch"):
                            verified = True
                            details += f" | window title '{active.title}' matches"
                            break
            except Exception:
                pass

        # --- Layer 3: File existence check (for file creation steps) ---
        if "create" in step_lower or "file" in step_lower or "document" in step_lower:
            # Extract file path from tool result
            import re
            path_match = re.search(r'[A-Z]:\\[^\s"\']+', str(tool_result))
            if path_match:
                filepath = path_match.group()
                if os.path.exists(filepath):
                    verified = True
                    details += f" | file exists: {filepath}"
                else:
                    details += f" | file NOT found: {filepath}"

        # --- Layer 4: Process check (for app-open steps) ---
        if not verified and ("open" in step_lower or "play" in step_lower):
            windows = screen_state.get("windows", [])
            for keyword in step_lower.split():
                if keyword in ("the", "my", "a", "an", "open", "play", "launch", "some"):
                    continue
                for w_title in windows:
                    if keyword.lower() in w_title.lower():
                        verified = True
                        details += f" | process/window found: {w_title}"
                        break
                if verified:
                    break

        # --- Layer 5: Web extraction (for browser steps) ---
        if not verified and ("browser" in step_lower or "web" in step_lower or "search" in step_lower):
            url = self._get_browser_url()
            if url:
                try:
                    from web_agent import web_read
                    web_content = web_read(url)[:500]
                    if web_content and len(web_content) > 50:
                        verified = True
                        details += f" | Web content available ({len(web_content)} chars)"
                except Exception as e:
                    logger.debug(f"Web extraction failed: {e}")

        # --- Layer 6: Negative check — explicit error indicators override success ---
        error_indicators = ["error", "failed", "not found", "denied", "crash",
                            "stopped working", "not responding", "blocked"]
        if any(ind in result_lower for ind in error_indicators):
            verified = False
            details += " | Negative check: error keywords in tool result"

        # --- Layer 7: Vision fallback (slow, only if needed) ---
        if not verified and screen_state:
            summary = screen_state.get("summary", "").lower()
            if any(ind in summary for ind in error_indicators):
                verified = False
                details += f" | Screen shows error: {summary[:100]}"
            elif screen_state.get("foreground", ""):
                # Something is visible — but only accept if no error in result
                if not any(ind in result_lower for ind in error_indicators):
                    verified = True
                    details += " | Foreground visible, no errors detected"

        return {"verified": verified, "details": details, "web_content": web_content}

    # ==================================================================
    # DIAGNOSE — multi-round LLM consultation on failure
    # ==================================================================

    def _diagnose(self, step, error, screen_state):
        """Multi-round LLM consultation when something goes wrong, with failure history.

        Enhanced with:
        - Reflexion pattern: checks stored reflections for similar failures
        - Web research: searches online when local diagnosis fails
        - Skill library: checks if a known skill handles this situation
        """
        # Collect ALL failures for this step
        step_failures = [h for h in self._history
                         if h.get("plan_step") == step
                         and ("error" in str(h.get("result", "")).lower()
                              or "not found" in str(h.get("result", "")).lower())]

        # Find matching recovery hint
        hint = ""
        for pattern, advice in _RECOVERY_HINTS.items():
            if pattern in error.lower():
                hint = f"\nRECOVERY HINT: {advice}"
                break

        # --- REFLEXION: Check stored reflections for similar failures ---
        reflexion_context = ""
        try:
            from skills import SkillLibrary
            skill_lib = SkillLibrary()
            # Search for skills related to this step
            matches = skill_lib.find_skill(step, min_similarity=0.4, limit=1)
            if matches:
                reflections = skill_lib.get_reflections(matches[0]["name"], limit=2)
                if reflections:
                    reflexion_context = "\nPREVIOUS LESSONS LEARNED:\n"
                    for r in reflections:
                        reflexion_context += f"  - {r['reflection']}\n"
        except Exception:
            pass

        prev_attempts = ""
        if step_failures:
            prev_attempts = f"Previous attempts for this step: {len(step_failures)}\n"
            for f in step_failures[-3:]:
                prev_attempts += f"  - {f.get('tool', '?')}: {f.get('result', '')[:80]}\n"

        prompt = (
            f"A desktop automation step FAILED. Help me fix it.\n"
            f"STEP: {step}\n"
            f"ERROR: {error}\n"
            f"{prev_attempts}"
            f"{hint}"
            f"{reflexion_context}\n"
            f"SCREEN: {screen_state.get('summary', 'unknown')}\n"
            f"WINDOW: {screen_state.get('foreground', 'unknown')}\n\n"
            f"What DIFFERENT approach should we try? Give a specific fix as JSON:\n"
            f'{{"diagnosis": "what went wrong", "fix_action": "tool_name", "fix_args": {{...}}}}\n'
            f"Available tools: {', '.join(AVAILABLE_TOOLS)}"
        )
        try:
            resp = requests.post(
                f"{OLLAMA_API}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 300},
                },
                timeout=20,
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            # Extract JSON — try balanced brace matching for first object
            brace_depth = 0
            start = content.find('{')
            if start >= 0:
                for i in range(start, len(content)):
                    if content[i] == '{':
                        brace_depth += 1
                    elif content[i] == '}':
                        brace_depth -= 1
                        if brace_depth == 0:
                            raw = content[start:i+1]
                            # Strip comments
                            raw = re.sub(r'//[^\n]*', '', raw)
                            raw = re.sub(r'#[^\n]*', '', raw)
                            raw = re.sub(r',\s*}', '}', raw)
                            raw = re.sub(r',\s*]', ']', raw)
                            fix = json.loads(raw)
                            # Validate fix_action against known tools
                            fix_tool = fix.get("fix_action")
                            if fix_tool and fix_tool not in AVAILABLE_TOOLS:
                                from difflib import get_close_matches
                                matches = get_close_matches(fix_tool, AVAILABLE_TOOLS, n=1, cutoff=0.5)
                                if matches:
                                    logger.info(f"Diagnosis fix_action corrected: {fix_tool} → {matches[0]}")
                                    fix["fix_action"] = matches[0]
                                else:
                                    logger.warning(f"Diagnosis suggested invalid tool: {fix_tool}")
                                    fix["fix_action"] = None
                            return fix
        except Exception as e:
            logger.warning(f"Diagnosis failed: {e}")

        # --- RESEARCH WHEN STUCK: if local diagnosis fails, search online ---
        if len(step_failures) >= 2:
            research_result = self._research_when_stuck(step, error, step_failures)
            if research_result and research_result.get("fix_action"):
                return research_result

        return {"diagnosis": "Unknown error", "fix_action": None, "fix_args": {}}

    def _research_when_stuck(self, step, error, step_failures):
        """Search the web for solutions when local diagnosis fails.

        Inspired by OpenHands/SWE-Agent: when stuck, research online.
        """
        try:
            from web_agent import research_solution
        except ImportError:
            logger.debug("web_agent.research_solution not available")
            return None

        # Build list of failed approaches
        failed_approaches = []
        for f in step_failures[-3:]:
            tool = f.get("tool", "?")
            result = f.get("result", "")[:80]
            failed_approaches.append(f"{tool}: {result}")

        logger.info(f"Researching solution online for: {step[:50]}")

        try:
            # Use LLM function if available
            llm_fn = None
            try:
                from cognitive import _llm_call
                llm_fn = _llm_call
            except ImportError:
                pass

            solution = research_solution(
                goal=step,
                error_message=error,
                failed_approaches=failed_approaches,
                llm_fn=llm_fn,
            )

            if solution and solution.get("confidence", 0) > 0.3 and solution.get("steps"):
                # Convert first research step into a tool action
                first_step = solution["steps"][0]
                logger.info(f"Research found solution (confidence={solution['confidence']:.0%}): {first_step[:80]}")

                # Try to map research step to a tool
                fix_action = None
                fix_args = {}

                # Pattern matching for common solution steps
                if re.search(r'\b(open|launch|start)\s+(.+)', first_step, re.I):
                    m = re.search(r'\b(?:open|launch|start)\s+(.+)', first_step, re.I)
                    fix_action = "open_app"
                    fix_args = {"name": m.group(1).strip()[:30]}
                elif re.search(r'\b(search|google|look up)\s+(.+)', first_step, re.I):
                    m = re.search(r'\b(?:search|google|look up)\s+(.+)', first_step, re.I)
                    fix_action = "google_search"
                    fix_args = {"query": m.group(1).strip()[:50]}
                elif re.search(r'\b(run|execute)\s+(.+)', first_step, re.I):
                    m = re.search(r'\b(?:run|execute)\s+(.+)', first_step, re.I)
                    fix_action = "run_command"
                    fix_args = {"command": m.group(1).strip()[:100]}
                elif re.search(r'\b(press|type|enter)\b', first_step, re.I):
                    fix_action = "press_key"
                    fix_args = {"key": "enter"}
                elif re.search(r'\b(click)\b', first_step, re.I):
                    fix_action = "click_control"
                    fix_args = {"name": first_step[:30]}

                if fix_action:
                    # Store reflection for future reference
                    self._store_reflexion(step, error, solution)
                    return {
                        "diagnosis": f"Web research: {solution.get('solution', '')[:100]}",
                        "fix_action": fix_action,
                        "fix_args": fix_args,
                        "from_research": True,
                    }

                # If we can't map to a tool, store the knowledge for future
                self._store_reflexion(step, error, solution)

        except Exception as e:
            logger.debug(f"Research-when-stuck failed: {e}")

        return None

    def _store_reflexion(self, step, error, solution):
        """Store a reflection from a failed step for future Reflexion retrieval.

        This is the Reflexion pattern: after failure, generate a verbal
        reflection about what went wrong and what to do differently.
        """
        try:
            from skills import SkillLibrary
            skill_lib = SkillLibrary()

            # Generate reflection text
            reflection = (
                f"Task '{step[:50]}' failed with: {error[:50]}. "
                f"Solution found: {solution.get('solution', '')[:100]}. "
                f"Steps: {'; '.join(solution.get('steps', [])[:3])}"
            )

            # Generate a skill name from the step
            name = skill_lib.generate_skill_name(step)
            skill_lib.add_reflection(name, reflection)
            logger.info(f"Reflexion stored for '{name}': {reflection[:80]}")
        except Exception as e:
            logger.debug(f"Failed to store reflexion: {e}")

    # ==================================================================
    # RUN COMMAND — execute terminal commands for system checks
    # ==================================================================

    def _run_terminal_command(self, command):
        """Run a terminal command and return output (for system checks)."""
        import subprocess
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=15
            )
            output = result.stdout.strip() or result.stderr.strip()
            return output[:2000] if output else "Command completed with no output."
        except subprocess.TimeoutExpired:
            return "Command timed out after 15 seconds."
        except Exception as e:
            return f"Command error: {e}"

    # ==================================================================
    # GET BROWSER URL — extract current URL from browser window
    # ==================================================================

    def _get_browser_url(self):
        """Get current URL from active browser window using address bar."""
        try:
            import pygetwindow as gw
            import pyautogui

            active = gw.getActiveWindow()
            if not active or not active.title:
                return None

            title_lower = active.title.lower()
            browser_keywords = ["firefox", "chrome", "edge", "brave", "opera"]
            if not any(b in title_lower for b in browser_keywords):
                return None

            # Copy URL from address bar: Ctrl+L, Ctrl+C, then read clipboard
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.2)
            pyautogui.press("escape")  # Deselect address bar

            # Read clipboard
            try:
                import subprocess
                result = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=5
                )
                url = result.stdout.strip()
                if url.startswith(("http://", "https://")):
                    return url
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Could not get browser URL: {e}")
        return None

    def _extract_browser_content(self):
        """Extract structured content from the current browser page.

        Gets URL + page text (links, headings, buttons) so the agent
        knows WHERE to click and WHAT elements exist on the page.
        Returns dict with url, title, links, text_snippet, or None if not in browser.
        """
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if not active or not active.title:
                return None

            title = active.title
            title_lower = title.lower()
            browser_keywords = ["firefox", "chrome", "edge", "brave", "opera"]
            if not any(b in title_lower for b in browser_keywords):
                return None

            # Get URL
            url = self._get_browser_url()
            if not url:
                return {"title": title, "url": None, "content": None, "links": []}

            # Fetch page content via web_read
            try:
                from web_agent import web_read
                import requests as _req

                raw_content = ""
                links = []
                try:
                    resp = _req.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }, timeout=5)
                    html = resp.text

                    # Extract links with text (for knowing what's clickable)
                    link_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.DOTALL)
                    for href, text in link_pattern.findall(html):
                        text_clean = re.sub(r'<[^>]+>', '', text).strip()
                        if text_clean and len(text_clean) > 2 and len(text_clean) < 200:
                            links.append({"text": text_clean[:80], "href": href[:200]})

                    # Extract buttons
                    btn_pattern = re.compile(r'<button[^>]*>(.*?)</button>', re.DOTALL)
                    for btn_text in btn_pattern.findall(html):
                        btn_clean = re.sub(r'<[^>]+>', '', btn_text).strip()
                        if btn_clean and len(btn_clean) > 1:
                            links.append({"text": f"[Button] {btn_clean[:60]}", "href": ""})

                    # Extract input fields
                    input_pattern = re.compile(r'<input[^>]+(?:placeholder|aria-label)=["\']([^"\']+)["\']', re.I)
                    for placeholder in input_pattern.findall(html):
                        links.append({"text": f"[Input] {placeholder[:60]}", "href": ""})

                except Exception as e:
                    logger.debug(f"Page fetch error: {e}")

                # Get readable text summary
                page_text = web_read(url)
                if page_text and len(page_text) > 50:
                    raw_content = page_text[:1000]

                # Detect forms on the page
                forms = []
                form_pattern = re.compile(r'<form[^>]*>(.*?)</form>', re.DOTALL | re.I)
                for form_html in form_pattern.findall(html):
                    form_fields = []
                    # Find labeled inputs
                    label_pattern = re.compile(
                        r'<label[^>]*>([^<]+)</label>\s*'
                        r'(?:<input[^>]+(?:name|id)=["\']([^"\']+)["\'])?',
                        re.I
                    )
                    for label, name in label_pattern.findall(form_html):
                        form_fields.append({"label": label.strip(), "name": name or label.strip()})

                    # Find inputs with placeholder/aria-label
                    inp_pattern = re.compile(
                        r'<input[^>]+(?:placeholder|aria-label)=["\']([^"\']+)["\'][^>]*'
                        r'(?:name=["\']([^"\']+)["\'])?',
                        re.I
                    )
                    for placeholder, name in inp_pattern.findall(form_html):
                        form_fields.append({"label": placeholder.strip(), "name": name or placeholder.strip()})

                    # Find submit buttons
                    submit_pattern = re.compile(r'<(?:button|input)[^>]+(?:type=["\']submit["\']|type=["\']button["\'])[^>]*(?:value=["\']([^"\']+)["\'])?[^>]*>([^<]*)', re.I)
                    for value, text in submit_pattern.findall(form_html):
                        btn_text = (value or text or "Submit").strip()
                        if btn_text:
                            form_fields.append({"label": f"[Submit] {btn_text}", "name": "__submit__"})

                    if form_fields:
                        forms.append(form_fields)

                # Deduplicate and limit links
                seen = set()
                unique_links = []
                for lnk in links:
                    key = lnk["text"].lower()
                    if key not in seen:
                        seen.add(key)
                        unique_links.append(lnk)
                    if len(unique_links) >= 20:
                        break

                return {
                    "title": title,
                    "url": url,
                    "content": raw_content,
                    "links": unique_links,
                    "forms": forms,
                }
            except Exception as e:
                logger.debug(f"Browser content extraction failed: {e}")
                return {"title": title, "url": url, "content": None, "links": []}

        except Exception as e:
            logger.debug(f"Browser content extraction error: {e}")
            return None

    # ==================================================================
    # OBSERVE — look at the screen and understand what's happening
    # ==================================================================

    def _get_window_inventory(self):
        """Get all visible windows with titles — fast OS-level ground truth."""
        try:
            import pygetwindow as gw
            windows = []
            for w in gw.getAllWindows():
                if w.title and w.visible and not w.isMinimized:
                    windows.append(w.title)
            return windows
        except Exception:
            return []

    def _get_running_apps(self):
        """Get key running processes (apps the user cares about)."""
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "STATUS eq Running", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            # Extract unique app names from CSV, skip system processes
            _SKIP = {"svchost.exe", "csrss.exe", "wininit.exe", "services.exe",
                      "lsass.exe", "dwm.exe", "conhost.exe", "RuntimeBroker.exe",
                      "tasklist.exe", "cmd.exe", "python.exe", "python3.12.exe"}
            apps = set()
            for line in result.stdout.strip().split("\n"):
                parts = line.strip('"').split('","')
                if parts and parts[0] not in _SKIP:
                    apps.add(parts[0])
            return sorted(apps)[:20]  # Top 20 to avoid noise
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Vision-need detection — returns True only for explicitly visual tasks
    # ------------------------------------------------------------------

    _VISUAL_PATTERNS = re.compile(
        r"find\s+(the\s+)?(red|blue|green|yellow|orange|white|black|grey|gray|purple|pink)?\s*"
        r"(button|icon|image|picture|logo|element|widget|badge|banner|avatar|thumbnail)"
        r"|what('s| is)\s+on\s+(the\s+)?screen"
        r"|what\s+do\s+(you|i)\s+see"
        r"|look\s+at\s+(the\s+)?screen"
        r"|click\s+(the\s+)?(image|picture|icon|logo|avatar|thumbnail)"
        r"|describe\s+(the\s+)?screen"
        r"|read\s+(the\s+)?text\s+(on|from)\s+(the\s+)?screen"
        r"|screenshot"
        r"|visual(ly)?"
        r"|color|colour"
        r"|pixel"
        r"|appears?\s+(on|in)\s+(the\s+)?screen",
        re.IGNORECASE,
    )

    def _vision_needed(self, goal):
        """Return True only for tasks that explicitly require visual inspection.

        Examples that return True:
          - "find the red button"
          - "what's on screen"
          - "click the image of the logo"
          - "describe the screen"

        Examples that return False:
          - "open Chrome and search for Python"
          - "play a song on Spotify"
          - "create a file on the desktop"
        """
        return bool(self._VISUAL_PATTERNS.search(goal))

    def _observe(self, goal, use_vision=False):
        """
        Build comprehensive screen state using ALL available methods combined.

        Always uses (every turn):
          1. OS-level: Active window title, visible windows, running processes
          2. UIA: Accessibility tree elements (clickable buttons, input fields, coords)
          3. CDP/Web: Browser URL, page content, links, buttons, forms (if browser running)
          4. API: App-specific state (e.g. Spotify playing status)

        Vision (screenshot + llava) is added ONLY when:
          - ``use_vision=True`` is passed explicitly
          - The agent is stuck (self._stuck_count >= 2)
          - The goal requires visual inspection (self._vision_needed(goal))
        """
        from vision import get_active_window_title

        # === LAYER 1: OS-level info (fast, always available) ===
        window_title = get_active_window_title()
        visible_windows = self._get_window_inventory()
        running_apps = self._get_running_apps()

        os_summary = f"Active window: {window_title}"
        if visible_windows:
            win_list = [w[:50] for w in visible_windows[:8]]
            os_summary += f"\nVisible windows: {', '.join(win_list)}"
        if running_apps:
            os_summary += f"\nRunning apps: {', '.join(running_apps[:15])}"

        # === LAYER 2: UIA — accessibility tree (precise clickable targets) ===
        ui_elements = []
        try:
            from computer import get_ui_elements
            ui_elements = get_ui_elements(max_depth=3, max_elements=25)
        except ImportError as e:
            logger.info(f"UIA layer unavailable (missing package): {e}")
        except Exception as e:
            logger.info(f"UIA layer failed: {e}")

        if ui_elements:
            ui_summary_parts = []
            for el in ui_elements[:15]:
                tag = "CLICK" if el.get("clickable") else "INPUT" if el.get("editable") else "ELEM"
                ui_summary_parts.append(f"[{tag}] \"{el['name']}\"")
            os_summary += f"\nUI elements: {', '.join(ui_summary_parts)}"

        # === LAYER 3: CDP/Web — browser content (always try, even if not focused) ===
        browser_content = None
        # Try browser content if ANY browser is running (not just focused)
        browser_names = ["chrome", "firefox", "msedge", "brave", "opera"]
        browser_running = any(
            app.lower().replace(".exe", "") in browser_names
            for app in running_apps
        )
        browser_focused = any(
            b in (window_title or "").lower()
            for b in ["firefox", "chrome", "edge", "brave", "opera"]
        )

        if browser_running or browser_focused:
            try:
                browser_content = self._extract_browser_content()
                if browser_content:
                    if browser_content.get("url"):
                        os_summary += f"\nBrowser URL: {browser_content.get('url', 'unknown')}"
                    if browser_content.get("content"):
                        os_summary += f"\nPage content: {browser_content['content'][:400]}"
                    if browser_content.get("links"):
                        link_names = [l["text"][:40] for l in browser_content["links"][:8]]
                        os_summary += f"\nClickable links: {', '.join(link_names)}"
            except Exception as e:
                logger.info(f"Browser content layer failed: {e}")

        # === LAYER 4: API / App-specific state ===
        try:
            # Spotify: detect current track from window title
            for win in visible_windows:
                if "spotify" in win.lower() and " - " in win:
                    # Spotify window title is "Song - Artist - Spotify"
                    os_summary += f"\nSpotify now playing: {win}"
                    break
            # YouTube: detect current video from browser title
            if browser_content and browser_content.get("url", ""):
                url = browser_content["url"]
                if "youtube.com/watch" in url:
                    # Title from browser is usually "Video Title - YouTube"
                    for win in visible_windows:
                        if "youtube" in win.lower():
                            os_summary += f"\nYouTube playing: {win}"
                            break
        except Exception:
            pass

        # === LAYER 5 (last resort): Vision — screenshot + llava ===
        need_vision = (
            use_vision
            or self._stuck_count >= 2
            or self._vision_needed(goal)
        )

        image = None
        b64 = None
        description = os_summary

        if need_vision:
            logger.info("Using vision (screenshot + llava) — %s",
                        "caller requested" if use_vision
                        else "agent stuck" if self._stuck_count >= 2
                        else "goal requires visual inspection")
            try:
                from vision import capture_screenshot, image_to_base64, _call_llava

                image = capture_screenshot()
                if image is not None:
                    b64 = image_to_base64(image)

                    prompt = (
                        "Describe this Windows screenshot in 1-2 SHORT sentences:\n"
                        "- What is the main app/window visible?\n"
                        "- Is there a popup, dialog box, or modal OVERLAYING the main window?\n"
                        "IMPORTANT: A terminal/command prompt/PowerShell window is NOT a blocker.\n"
                        "Only report popups/dialogs that are clearly blocking something else."
                    )
                    vision_desc = _call_llava(prompt, b64, temperature=0.1, num_predict=150)
                    if vision_desc:
                        description = f"{os_summary}\nVision: {vision_desc}"
            except Exception as e:
                logger.warning(f"Vision fallback failed: {e}")
        else:
            logger.info("Observation: OS + UIA + Web + API (no vision needed)")

        if description is None:
            description = "Vision model did not respond."

        desc_lower = description.lower()

        # Detect REAL blockers (not terminals, not normal app windows)
        blocked = False
        blocker_phrases = [
            "popup", "dialog box", "modal",
            "profile picker", "choose profile", "select profile",
            "default browser", "not your default", "set as default",
            "cookie banner", "cookie consent", "accept cookies",
            "sign in required", "login required",
            "choose an app", "how do you want to open",
            "overlay", "blocking",
        ]
        not_blockers = [
            "terminal", "command prompt", "powershell", "cmd",
            "desktop", "taskbar", "start menu",
        ]
        for kw in blocker_phrases:
            if kw in desc_lower:
                is_false_positive = any(nb in desc_lower for nb in not_blockers)
                if not is_false_positive:
                    blocked = True
                    break

        return {
            "summary": description.strip(),
            "blocked": blocked,
            "foreground": window_title,
            "windows": visible_windows,
            "processes": running_apps,
            "raw": description,
            "image": image,
            "image_b64": b64,
            "browser_content": browser_content,
            "ui_elements": ui_elements,
        }

    # ==================================================================
    # THINK — decide what to do next based on observation
    # ==================================================================

    def _think(self, goal, screen_state):
        """
        Ask the LLM: given what I see on screen and my goal,
        what should I do next?

        Returns a decision dict with: action, tool, args, reasoning, summary
        """
        history_text = ""
        if self._history:
            recent = self._history[-5:]  # Last 5 turns for context
            lines = []
            for h in recent:
                tool = h.get('tool', '?')
                args = h.get('args', {})
                result = h.get('result', '')[:100]
                lines.append(f"  Turn {h['turn']}: {tool}({json.dumps(args)}) → RESULT: {result}")
            history_text = "PREVIOUS ACTIONS AND RESULTS:\n" + "\n".join(lines) + "\n\n"

        screen_summary = screen_state.get("summary", "unknown")
        window_title = screen_state.get("foreground", "unknown")
        is_blocked = screen_state.get("blocked", False)
        stuck_warning = screen_state.get("stuck_warning", "")
        completed_actions = screen_state.get("completed_actions", "")
        current_plan_step = screen_state.get("current_plan_step", "")
        remaining_plan = screen_state.get("remaining_plan", [])

        plan_context = ""
        if current_plan_step:
            plan_context = (
                f"CURRENT PLAN STEP: {current_plan_step}\n"
                f"REMAINING STEPS: {', '.join(remaining_plan[1:]) if len(remaining_plan) > 1 else 'none'}\n\n"
            )

        # Build structured progress summary (attention recitation)
        progress_summary = self._build_progress_summary()

        # Build browser context if available
        browser_ctx = ""
        browser_data = screen_state.get("browser_content")
        if browser_data:
            browser_ctx = f"\nBROWSER PAGE DATA:\n"
            if browser_data.get("url"):
                browser_ctx += f"  URL: {browser_data['url']}\n"
            if browser_data.get("links"):
                link_lines = []
                for i, lnk in enumerate(browser_data["links"][:15]):
                    link_lines.append(f"    {i+1}. {lnk['text']}")
                browser_ctx += f"  Clickable elements:\n" + "\n".join(link_lines) + "\n"
            if browser_data.get("content"):
                browser_ctx += f"  Page text: {browser_data['content'][:500]}\n"
            if browser_data.get("forms"):
                for i, form in enumerate(browser_data["forms"][:3]):
                    field_lines = []
                    for f in form:
                        field_lines.append(f"      - {f['label']}")
                    browser_ctx += f"  Form {i+1} fields:\n" + "\n".join(field_lines) + "\n"
                browser_ctx += (
                    f"  TIP: Use fill_form to fill detected forms. Example:\n"
                    f'  fill_form({{"fields": {{"field_name": "value", ...}}}})\n'
                )
            browser_ctx += (
                f"  TIP: Use this data to decide what to click. "
                f"Find the element by text, then use click_element or click_at.\n"
                f"  For YouTube: look for video titles (not 'Ad' items). "
                f"Click the title text of the actual video, not ads.\n\n"
            )

        # Build UI Automation context (precise clickable elements with coordinates)
        ui_ctx = ""
        ui_els = screen_state.get("ui_elements", [])
        if ui_els:
            ui_lines = []
            for el in ui_els[:15]:
                tag = "CLICK" if el.get("clickable") else "INPUT" if el.get("editable") else "ELEM"
                ui_lines.append(f"    [{tag}] \"{el['name']}\" at ({el['x']}, {el['y']})")
            ui_ctx = (
                f"\nUI ELEMENTS (precise coordinates from accessibility tree):\n"
                + "\n".join(ui_lines) + "\n"
                f"  TIP: Use click_at with these EXACT coordinates for reliable clicking.\n"
                f"  For input fields marked [INPUT], click at coords then use type_text.\n\n"
            )

        prompt = (
            f"You are a desktop automation agent on Windows. Decide the NEXT action.\n\n"
            f"{stuck_warning + chr(10) + chr(10) if stuck_warning else ''}"
            f"{('ALREADY DONE: ' + completed_actions + chr(10) + chr(10)) if completed_actions else ''}"
            f"--- PROGRESS ---\n{progress_summary}--- END PROGRESS ---\n\n"
            f"{plan_context}"
            f"WHAT I SEE: {screen_summary}\n"
            f"Active window: \"{window_title}\"\n"
            f"{browser_ctx}"
            f"{ui_ctx}"
            f"Turn: {self._turn_count}/{MAX_AGENT_TURNS}\n\n"
            f"TOOLS (prefer direct tools over UI clicking — they're faster and more reliable):\n"
            f'- open_app: {{"name": "Notepad"}} — opens any app\n'
            f'- close_app: {{"name": "Notepad"}}\n'
            f'- search_in_app: {{"app": "YouTube", "query": "music"}} — opens AND searches\n'
            f'- google_search: {{"query": "search terms"}}\n'
            f'- type_text: {{"text": "hello"}}\n'
            f'- press_key: {{"keys": "enter"}} or {{"keys": "escape"}}\n'
            f'- click_at: {{"x": 500, "y": 300}} — only when no direct tool exists\n'
            f'- scroll: {{"direction": "down"}}\n'
            f'- focus_window: {{"name": "Notepad"}}\n'
            f'- run_terminal: {{"command": "Get-PSDrive C"}} — PowerShell for system info, disk, processes, network\n'
            f'- run_command: {{"command": "tasklist"}} — simple terminal command\n'
            f'- manage_files: {{"action": "move", "path": "Desktop/file.txt", "destination": "Documents/"}} — file operations\n'
            f'- manage_software: {{"action": "install", "name": "VLC"}} — install/uninstall via winget\n'
            f'- get_weather: {{"city": "London"}} — weather info\n'
            f'- create_file: {{"path": "test.py", "content": "print(1)"}} — create files\n'
            f'- toggle_setting: {{"setting": "bluetooth", "state": "on"}} — system settings\n'
            f'- play_music: {{"action": "play", "query": "jazz"}} — music control\n'
            f'- web_read: {{"url": "https://example.com"}} — read page content for understanding\n'
            f'- find_on_screen: {{"description": "Skip Ad button"}} — find element by description\n'
            f'- click_element: {{"name": "Skip Ad"}} — click UI element by exact name (uses accessibility tree, most precise)\n'
            f'- manage_tabs: {{"action": "new|close|next|prev|goto|list"}} — browser tab management\n'
            f'- fill_form: {{"fields": {{"username": "john", "password": "1234"}}}} — fill form fields by name\n'
            f"{self._get_openclaw_tools_prompt()}\n"
            f"IMPORTANT RULES:\n"
            f"- ALWAYS prefer direct tools over UI clicking — they're faster and more reliable.\n"
            f"  * System info (disk, RAM, CPU, processes, network) → run_terminal\n"
            f"  * Install/uninstall software → manage_software\n"
            f"  * Move/copy/delete files → manage_files\n"
            f"  * Weather/time/news → get_weather/get_time/get_news\n"
            f"  * Only use click_at when NO direct tool exists for the action.\n"
            f"- Focus on PROGRESSING toward the goal. What is the NEXT step?\n"
            f"- TRUST tool results: if open_app said 'opened', the app IS open.\n"
            f"  If type_text said 'Typed N characters', the text IS typed. Move on.\n"
            f"  If search_in_app said 'Searching', the search IS done.\n"
            f"- A terminal/command prompt in the background is NOT a problem. Ignore it.\n"
            f"- If a REAL popup/dialog is blocking, dismiss with press_key escape or enter\n"
            f"- If wrong window is focused, use focus_window first\n"
            f"- When ALL parts of the goal are done, say DONE immediately\n"
            f"- Do NOT repeat an action that already succeeded\n"
            f"- search_in_app opens the app AND searches — no need to focus or open first!\n"
            f"- Only use focus_window when you need to interact with an ALREADY OPEN window\n"
            f"APP-SPECIFIC TIPS:\n"
            f"- Spotify: use play_music with action='play_query' — handles search + UIA accessibility click.\n"
            f"  search_in_app also works — searches via URI protocol + clicks via accessibility tree.\n"
            f"  Spotify is a desktop app — do NOT try to open it via browser.\n"
            f"- YouTube: use play_music with app='youtube' — handles CDP browser automation to click videos.\n"
            f"  search_in_app also works — opens YouTube search and auto-clicks first non-ad video via CDP.\n"
            f"- For web pages: prefer browser_action (uses CDP) over click_at. It clicks by CSS selector or text.\n"
            f"- For desktop apps: prefer click_control (uses UIA) over click_at. It finds elements by name.\n"
            f"- NEVER use click_at with guessed coordinates. Use click_control or browser_action instead.\n"
            f"BROWSER INTERACTION RULES:\n"
            f"- Use browser_action for ALL web page interactions — it uses CDP (Chrome DevTools Protocol).\n"
            f"  browser_action can: navigate, click by CSS selector/text, fill forms, read page, run JS.\n"
            f"- To click a link: browser_action with action='click', text='link text'\n"
            f"- To fill a form: browser_action with action='fill', selector='#field', text='value'\n"
            f"- To read page content: browser_action with action='read'\n"
            f"- NEVER use click_at for web pages. browser_action is faster and more reliable.\n"
            f"- For YouTube: browser_action to click video titles. Skip 'Ad'/'Sponsored' results.\n"
            f"- If page data shows a 'Skip Ad' button, click it with browser_action.\n\n"
            f"DESKTOP APP INTERACTION RULES:\n"
            f"- Use click_control for ALL desktop app interactions — it uses UIA (accessibility tree).\n"
            f"  click_control finds buttons, links, list items by name — no coordinates needed.\n"
            f"- Use inspect_window to see available controls before clicking.\n"
            f"- NEVER use click_at for desktop apps unless click_control fails.\n"
            f"- Windows Settings:\n"
            f"  * open_app with 'bluetooth' opens Bluetooth settings directly\n"
            f"  * open_app with 'wifi' opens WiFi settings directly\n"
            f"  * To toggle a switch: use click_control with the toggle's name\n"
            f"  * Bluetooth/WiFi toggles are usually near the top of the page\n"
            f"{self._get_memory_hints()}\n"
            f'Respond with ONLY one JSON object:\n'
            f'{{"action": "USE_TOOL", "tool": "name", "args": {{...}}, "reasoning": "why"}}\n'
            f'{{"action": "DONE", "summary": "what was done", "reasoning": "why done"}}\n'
            f'{{"action": "GIVE_UP", "reasoning": "why stuck"}}\n'
        )

        try:
            resp = requests.post(
                f"{OLLAMA_API}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 400},
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]

            # Parse JSON from response
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                decision = json.loads(json_match.group())
            else:
                decision = json.loads(content)

            # Validate action
            action = decision.get("action", "")
            if action not in ("USE_TOOL", "DONE", "GIVE_UP"):
                # LLM gave some other format — try to interpret
                if "tool" in decision:
                    decision["action"] = "USE_TOOL"
                elif any(w in content.lower() for w in ["done", "complete", "success", "achieved"]):
                    decision["action"] = "DONE"
                    decision["summary"] = decision.get("reasoning", "Task completed.")
                else:
                    decision["action"] = "GIVE_UP"
                    decision["reasoning"] = f"Unclear LLM response: {content[:100]}"

            return decision

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse agent decision: {e}")
            # Fallback: if screen is blocked, try escape. Otherwise give up.
            if screen_state.get("blocked"):
                return {"action": "USE_TOOL", "tool": "press_key",
                        "args": {"keys": "escape"},
                        "reasoning": "Parse failed, screen blocked, trying escape"}
            return {"action": "GIVE_UP", "reasoning": f"Could not decide: {e}"}
        except Exception as e:
            logger.error(f"Agent think failed: {e}")
            return {"action": "GIVE_UP", "reasoning": f"Thinking error: {e}"}

    # ==================================================================
    # ACT — execute the decided action
    # ==================================================================

    def _parse_result(self, tool_name, args, raw_result):
        """Parse a tool result string into a structured outcome."""
        result_str = str(raw_result).lower()
        outcome = {
            "status": "unknown",
            "evidence": str(raw_result)[:200],
            "next_hint": "",
            "raw": raw_result,
        }

        # Detect success
        success_words = ["opened", "launched", "created", "typed", "searched",
                         "clicked", "scrolled", "pressed", "playing", "toggled",
                         "focused", "closed"]
        if any(w in result_str for w in success_words):
            outcome["status"] = "success"

        # Detect failures
        fail_words = ["error", "not found", "failed", "couldn't", "timeout",
                       "blocked", "denied", "not installed"]
        if any(w in result_str for w in fail_words):
            outcome["status"] = "fail"
            # Suggest next action based on failure type
            if "not found" in result_str:
                outcome["next_hint"] = f"App not found. Try search_in_app or check installed apps."
            elif "timeout" in result_str:
                outcome["next_hint"] = f"Action timed out. Try a simpler approach."
            elif "error" in result_str:
                outcome["next_hint"] = f"Try alternative tool from escalation map."

        # Detect partial success
        if "but" in result_str or "however" in result_str or "partially" in result_str:
            outcome["status"] = "partial"
            outcome["next_hint"] = "Partially done — verify what's missing and complete."

        if outcome["status"] == "unknown":
            outcome["status"] = "success"  # Assume success if no failure indicators

        return outcome

    # Per-tool timeout limits (seconds)
    _TOOL_TIMEOUTS = {
        "click_at": 3, "press_key": 3, "type_text": 5, "scroll": 3,
        "focus_window": 5, "open_app": 10, "close_app": 5,
        "search_in_app": 15, "google_search": 10, "run_command": 15,
        # Brain tools
        "run_terminal": 35, "manage_files": 15, "manage_software": 120,
        "get_weather": 10, "get_time": 3, "get_news": 10, "get_forecast": 10,
        "create_file": 90, "toggle_setting": 10, "play_music": 10,
        "set_reminder": 5, "web_read": 15,
        # Precision interaction
        "click_element": 5, "find_on_screen": 10, "manage_tabs": 5, "fill_form": 10,
    }

    def _act(self, decision):
        """Execute the action decided by the think step, with per-tool timeout.

        Tries state-first orchestrator before falling back to brain.execute_tool.
        """
        from brain import execute_tool
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        tool_name = decision.get("tool", "")
        args = decision.get("args", {})

        if not tool_name:
            return "No tool specified"

        # --- STRATEGY PRE-CHECK: try fastest method first ---
        # Before using the standard tool dispatch, check if a faster
        # strategy (CLI, API, UIA, CDP) can handle this action directly.
        if tool_name and tool_name not in ("take_screenshot", "find_on_screen", "agent_task"):
            try:
                from execution_strategies import get_selector
                selector = get_selector()
                step_desc = decision.get("reasoning", "") or f"{tool_name} {json.dumps(args)[:80]}"
                fast_result, strategy_used = selector.execute_step(
                    step_desc, action_registry=self.action_registry, skip_vision=True)
                if fast_result and strategy_used:
                    self._think_log(f"Fast strategy '{strategy_used}' succeeded")
                    return fast_result
            except Exception as e:
                logger.debug(f"Strategy pre-check in _act: {e}")

        # Pre-action validation: check tool exists
        if (tool_name not in AVAILABLE_TOOLS
                and tool_name != "focus_window"
                and not tool_name.startswith("openclaw_")):
            # Try fuzzy match
            from difflib import get_close_matches
            matches = get_close_matches(tool_name, AVAILABLE_TOOLS, n=1, cutoff=0.5)
            if matches:
                logger.info(f"Agent corrected tool: {tool_name} → {matches[0]}")
                tool_name = matches[0]
                decision["tool"] = tool_name
            else:
                return f"Unknown tool: {tool_name}. Available: {', '.join(AVAILABLE_TOOLS)}"

        # Handle focus_window as a special action (not in brain registry)
        if tool_name == "focus_window":
            return self._focus_window(args.get("name", ""))

        # Handle precision interaction tools directly (computer.py functions)
        if tool_name == "click_element":
            from computer import click_element_by_name
            return click_element_by_name(args.get("name", ""))

        if tool_name == "manage_tabs":
            from computer import manage_tabs
            return manage_tabs(args.get("action", "list"), args.get("index"))

        if tool_name == "fill_form":
            from computer import fill_form_fields
            fields = args.get("fields", {})
            return fill_form_fields(fields)

        if tool_name == "find_on_screen":
            from computer import get_ui_elements
            element_name = args.get("description", args.get("element", args.get("name", "")))
            # Try accessibility tree first
            elements = get_ui_elements(max_elements=40)
            name_lower = element_name.lower()
            for el in elements:
                if name_lower in el["name"].lower():
                    return f"Found '{el['name']}' ({el['type']}) at ({el['x']}, {el['y']})"
            # Fallback to vision
            from vision import find_element
            result = find_element(element_name)
            if result.get("found"):
                return f"Found at ({result['x']}, {result['y']}): {result.get('description', '')}"
            return f"Not found: {result.get('description', 'element not visible')}"

        # Handle OpenClaw tools if available
        if tool_name.startswith("openclaw_") and tool_name in self._openclaw_tools:
            return self._openclaw_tools[tool_name](args)

        # Pre-action intelligence
        self._pre_action_hook(tool_name, args)

        # Normalize args for common issues
        args = self._normalize_args(tool_name, args, decision)

        # State-first: try orchestrator before vision-based execution
        try:
            from automation.orchestrator import StatefulOrchestrator
            if not hasattr(self, '_orchestrator'):
                self._orchestrator = StatefulOrchestrator(self.action_registry)
            if self._orchestrator.can_handle(tool_name, args):
                orch_result = self._orchestrator.execute(tool_name, args)
                if orch_result and orch_result.ok:
                    logger.info(f"Agent: orchestrator handled {tool_name} "
                               f"via {orch_result.strategy_used} "
                               f"({orch_result.duration_ms}ms)")
                    return orch_result.message or str(orch_result.state_after)
                elif orch_result:
                    logger.debug(f"Orchestrator failed for {tool_name}: "
                               f"{orch_result.error}, falling back to brain")
        except Exception as e:
            logger.debug(f"Orchestrator unavailable in agent: {e}")

        # Execute with per-tool timeout watchdog
        timeout = self._TOOL_TIMEOUTS.get(tool_name, 15)
        # Progress message for long operations
        if tool_name == "create_file" and timeout > 30:
            import threading
            def _progress():
                time.sleep(10)
                self._speak("Still writing the code...")
            progress_thread = threading.Thread(target=_progress, daemon=True)
            progress_thread.start()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(execute_tool, tool_name, args,
                                     self.action_registry, self.reminder_mgr)
                result = future.result(timeout=timeout)
        except FuturesTimeout:
            logger.warning(f"Tool {tool_name} timed out after {timeout}s")
            result = f"TIMEOUT: {tool_name} took longer than {timeout}s"
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}")
            result = f"ERROR: {tool_name} failed: {e}"

        # Post-action: handle known dialogs (save prompts, profile pickers)
        self._post_action_hook(tool_name, args, result)

        return result

    def _pre_action_hook(self, tool_name, args):
        """Handle known pre-action patterns."""
        try:
            from computer import press_key as _press_key
            import pygetwindow as gw

            # Before typing or pressing keys: verify correct window is focused
            if tool_name in ("type_text", "press_key"):
                active = gw.getActiveWindow()
                # Determine expected target from plan context
                target_app = self._get_target_app()
                if target_app and active and active.title:
                    title_lower = active.title.lower()
                    target_lower = target_app.lower()
                    # If active window doesn't match target, try to focus it
                    if target_lower not in title_lower:
                        logger.warning(f"Wrong window '{active.title}' for target '{target_app}', refocusing")
                        self._focus_window(target_app)
                        # Re-verify after refocus — abort if still wrong
                        time.sleep(0.3)
                        active2 = gw.getActiveWindow()
                        if active2 and active2.title:
                            if target_lower not in active2.title.lower():
                                logger.warning(f"Refocus failed — still on '{active2.title}', aborting type_text")
                                raise ValueError(f"Cannot type: wrong window '{active2.title}' (expected '{target_app}')")

            # Before typing in text editors: select all + delete to clear
            if tool_name == "type_text":
                active = gw.getActiveWindow()
                if active and active.title:
                    title_lower = active.title.lower()
                    # Only clear for text editors, not browsers/other apps
                    if any(kw in title_lower for kw in ["notepad", "untitled", ".txt"]):
                        import pyautogui
                        pyautogui.hotkey("ctrl", "a")
                        time.sleep(0.1)
                        pyautogui.press("delete")
                        time.sleep(0.1)
                        logger.info("Pre-action: cleared existing text in text editor")
        except Exception as e:
            logger.debug(f"Pre-action hook error: {e}")

    def _get_target_app(self):
        """Extract target app name from current plan step or recent history."""
        # Check current plan step for app context
        if self._plan_steps:
            current_idx = min(self._turn_count, len(self._plan_steps) - 1)
            step = self._plan_steps[current_idx].lower() if current_idx < len(self._plan_steps) else ""
            # Extract app name from step description
            app_keywords = {
                "notepad": "Notepad", "chrome": "Chrome", "firefox": "Firefox",
                "edge": "Edge", "spotify": "Spotify", "discord": "Discord",
                "vscode": "VS Code", "code": "VS Code", "terminal": "Terminal",
                "powershell": "PowerShell", "cmd": "Command Prompt",
                "word": "Word", "excel": "Excel", "explorer": "Explorer",
            }
            for kw, name in app_keywords.items():
                if kw in step:
                    return name
        # Check if we recently opened an app
        for h in reversed(self._history[-5:]):
            tool = h.get("tool", "")
            if tool == "open_app":
                return h.get("args", {}).get("name", "")
            if tool == "focus_window":
                return h.get("args", {}).get("name", "")
        return None

    def _post_action_hook(self, tool_name, args, result):
        """Handle common post-action patterns: wait-for-ready, dialogs, ads."""
        try:
            import pygetwindow as gw

            # Wait-for-ready: after opening apps, wait until window is actually active
            if tool_name == "open_app":
                app_name = args.get("name", "").lower()
                self._wait_for_app_ready(app_name)

                # Firefox profile picker
                if "firefox" in app_name:
                    active = gw.getActiveWindow()
                    if active and "profile" in active.title.lower():
                        import pyautogui
                        pyautogui.press("enter")
                        time.sleep(1)
                        logger.info("Post-action: dismissed Firefox profile picker")

            # After closing an app, handle "Save" dialog
            if tool_name == "close_app":
                time.sleep(0.5)
                active = gw.getActiveWindow()
                if active and active.title:
                    title_lower = active.title.lower()
                    if any(kw in title_lower for kw in ["save", "do you want"]):
                        import pyautogui
                        pyautogui.hotkey("alt", "n")
                        time.sleep(0.3)
                        logger.info("Post-action: dismissed save dialog with Don't Save")

            # After search_in_app — wait for results to load
            if tool_name == "search_in_app":
                time.sleep(1.5)  # Search results need time to populate

            # After YouTube navigation — handle pre-roll video ads
            if tool_name in ("search_in_app", "open_app", "click_at", "press_key"):
                active2 = gw.getActiveWindow()
                if active2 and "youtube" in (active2.title or "").lower():
                    self._handle_youtube_ads()
        except Exception as e:
            logger.debug(f"Post-action hook error: {e}")

    def _wait_for_app_ready(self, app_name, timeout=5):
        """Wait until a window matching app_name appears and is active.
        Prevents cascading failures from acting before app is loaded."""
        if not app_name:
            return
        import pygetwindow as gw
        start = time.time()
        while time.time() - start < timeout:
            try:
                active = gw.getActiveWindow()
                if active and active.title:
                    if app_name.lower() in active.title.lower():
                        logger.info(f"App ready: {active.title} (waited {time.time()-start:.1f}s)")
                        return
                # Also check all windows (app might not be active yet)
                for w in gw.getAllWindows():
                    if w.title and app_name.lower() in w.title.lower() and w.visible:
                        try:
                            w.activate()
                        except Exception:
                            pass
                        time.sleep(0.3)
                        logger.info(f"App found+activated: {w.title} (waited {time.time()-start:.1f}s)")
                        return
            except Exception:
                pass
            time.sleep(0.5)
        logger.warning(f"App '{app_name}' not ready after {timeout}s")

    def _normalize_args(self, tool_name, args, decision):
        """Fix common arg issues from the LLM."""
        if tool_name == "open_app" and "name" not in args:
            args["name"] = args.get("app", args.get("application",
                           args.get("url", args.get("website", ""))))

        if tool_name == "search_in_app":
            if "app" not in args:
                args["app"] = args.get("name", args.get("application",
                              args.get("website", args.get("platform", ""))))
            if "query" not in args:
                args["query"] = args.get("search", args.get("text",
                                args.get("search_query", args.get("term", ""))))
            # Fallback: extract from reasoning
            if not args.get("app") or not args.get("query"):
                reasoning = decision.get("reasoning", "")
                m = re.search(r'(?:search\s+(?:for\s+)?)(.*?)(?:\s+(?:in|on)\s+)(.*)', reasoning, re.I)
                if m:
                    if not args.get("query"):
                        args["query"] = m.group(1).strip().strip('"\'')
                    if not args.get("app"):
                        args["app"] = m.group(2).strip().strip('"\'')

        if tool_name == "press_key":
            if "keys" not in args:
                args["keys"] = args.get("key", args.get("combo",
                               args.get("hotkey", "enter")))

        if tool_name == "click_at":
            # If no coordinates, try vision to find the element
            if args.get("x", 0) == 0 and args.get("y", 0) == 0:
                target = decision.get("reasoning", "button")
                try:
                    from vision import find_element
                    found = find_element(target)
                    if found.get("found"):
                        args["x"] = found["x"]
                        args["y"] = found["y"]
                except Exception:
                    pass

        if tool_name == "scroll" and "direction" not in args:
            args["direction"] = "down"

        return args

    def _handle_youtube_ads(self):
        """Detect and skip YouTube pre-roll video ads.

        After navigating to a YouTube video, checks if an ad is playing
        and attempts to skip it using multiple strategies:
        1. Accessibility tree (click_element "Skip Ad")
        2. Vision-based text finding
        3. Keyboard navigation (Tab + Enter)
        """
        try:
            import pyautogui
            import pygetwindow as gw
            time.sleep(2)  # Wait for video page to load

            active = gw.getActiveWindow()
            if not active or "youtube" not in (active.title or "").lower():
                return

            for attempt in range(3):  # Check 3 times over ~12s
                # Strategy 1: Use accessibility tree to find Skip button
                try:
                    from computer import click_element_by_name
                    result = click_element_by_name("Skip Ad")
                    if "Clicked" in result:
                        logger.info(f"YouTube: skipped ad via accessibility tree: {result}")
                        return
                except Exception:
                    pass

                # Strategy 2: Use vision to find "Skip Ad" text
                try:
                    from vision import find_text_on_screen
                    skip = find_text_on_screen("Skip Ad")
                    if skip and skip.get("found"):
                        pyautogui.click(skip["x"], skip["y"])
                        logger.info("YouTube: clicked Skip Ad via vision OCR")
                        return
                except Exception:
                    pass

                # Strategy 3: Keyboard — Tab to skip button and Enter
                pyautogui.press("tab")
                time.sleep(0.3)

                time.sleep(3)

            logger.info("YouTube: no skip ad button found (might not be an ad)")
        except Exception as e:
            logger.debug(f"YouTube ad handler error: {e}")

    def _focus_window(self, name):
        """Bring a window to the foreground by name. Uses smart matching."""
        if not name:
            return "Error: no window name"
        try:
            import pygetwindow as gw
            name_lower = name.lower().strip()

            # Try exact match first
            windows = gw.getWindowsWithTitle(name)

            # Then partial match (e.g. "YouTube" matches "YouTube - Firefox")
            if not windows:
                all_windows = gw.getAllWindows()
                for w in all_windows:
                    if w.title and name_lower in w.title.lower():
                        windows = [w]
                        break

            # Try matching app names in window titles
            # e.g. "Firefox" for a YouTube tab, "Chrome" for Gmail tab
            if not windows:
                browser_names = ["firefox", "chrome", "edge", "brave", "opera"]
                app_keywords = {
                    "youtube": browser_names,
                    "gmail": browser_names,
                    "google": browser_names,
                    "wikipedia": browser_names,
                    "reddit": browser_names,
                    "settings": ["settings"],
                    "bluetooth": ["settings"],
                    "calculator": ["calculator"],
                    "notepad": ["notepad"],
                }
                # Check if the requested name is something that opens in a browser
                search_keywords = app_keywords.get(name_lower, [name_lower])
                all_windows = gw.getAllWindows()
                for w in all_windows:
                    if not w.title:
                        continue
                    title_lower = w.title.lower()
                    for kw in search_keywords:
                        if kw in title_lower:
                            windows = [w]
                            break
                    if windows:
                        break

            # Process-based fallback: find window by process name (e.g. Spotify.exe)
            if not windows:
                _PROCESS_TO_WINDOW = {
                    "spotify": "Spotify.exe", "discord": "Discord.exe",
                    "steam": "steam.exe", "slack": "slack.exe",
                }
                exe = _PROCESS_TO_WINDOW.get(name_lower)
                if exe:
                    try:
                        import subprocess as _sp
                        proc = _sp.run(["powershell", "-NoProfile", "-Command",
                                        f"Get-Process -Name '{exe.replace('.exe','')}' -ErrorAction SilentlyContinue | "
                                        f"Select-Object -ExpandProperty MainWindowTitle"],
                                       capture_output=True, text=True, timeout=5)
                        titles = [t.strip() for t in proc.stdout.strip().split("\n") if t.strip()]
                        if titles:
                            all_windows = gw.getAllWindows()
                            for w in all_windows:
                                if w.title and w.title.strip() in titles:
                                    windows = [w]
                                    logger.info(f"Process-based match: '{name}' → '{w.title}'")
                                    break
                    except Exception as e:
                        logger.debug(f"Process-based window lookup failed: {e}")

            if windows:
                win = windows[0]
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(0.5)
                return f"Focused window: {win.title}"
            return f"Window '{name}' not found"
        except Exception as e:
            return f"Error focusing window: {e}"

    # ==================================================================
    # Parallel sub-agents
    # ==================================================================

    def _detect_parallel_subtasks(self, goal):
        """
        Check if the goal has independent subtasks that can run in parallel.
        E.g., "open Notepad and type hello, and also search YouTube for music"
        """
        # Quick heuristic: conjunctions separating clearly different tasks
        separators = [
            r'\band\s+also\b', r'\bwhile\s+also\b', r'\bsimultaneously\b',
            r'\bat\s+the\s+same\s+time\b', r'\bin\s+parallel\b',
            r'\bplus\s+also\b',
        ]
        for sep in separators:
            parts = re.split(sep, goal, flags=re.I)
            if len(parts) > 1:
                return [p.strip() for p in parts if p.strip()]

        # Ask LLM if this has independent subtasks
        prompt = (
            f"Does this task have 2+ INDEPENDENT subtasks that can run in parallel?\n"
            f"Task: {goal}\n\n"
            f"Rules:\n"
            f"- Only split if tasks are truly INDEPENDENT (don't need each other)\n"
            f"- 'Open X and do Y in X' is ONE task (Y depends on X being open)\n"
            f"- 'Open X and type hello, also open Y and search' is TWO tasks\n\n"
            f"If YES, respond: SPLIT: [\"task1\", \"task2\"]\n"
            f"If NO, respond: SINGLE\n"
            f"Respond with ONLY SPLIT or SINGLE."
        )

        try:
            resp = requests.post(
                f"{OLLAMA_API}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 200},
                },
                timeout=15,
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"].strip()

            if content.upper().startswith("SINGLE"):
                return None

            # Try to parse the split
            match = re.search(r'\[[\s\S]*\]', content)
            if match:
                tasks = json.loads(match.group())
                if isinstance(tasks, list) and len(tasks) > 1:
                    return tasks

        except Exception as e:
            logger.warning(f"Parallel detection failed: {e}")

        return None

    def _execute_parallel(self, subtasks, original_goal):
        """
        Spawn sub-agents for independent subtasks and run them in parallel.
        Each sub-agent gets its own agentic loop.
        """
        self._think_log(f"Splitting into {len(subtasks)} independent tasks...")
        logger.info(f"Spawning {len(subtasks)} sub-agents: {subtasks}")

        results = {}

        def run_subtask(task_goal, task_id):
            """Run a single subtask in its own agentic loop."""
            logger.info(f"Sub-agent {task_id} starting: {task_goal}")
            # Each sub-agent gets a fresh agent instance but shares the registry
            sub_agent = DesktopAgent(
                self.action_registry, self.reminder_mgr,
                self.ollama_model, speak_fn=None,  # Sub-agents don't speak
            )
            result = sub_agent._agentic_loop(task_goal)
            return result

        # Run sub-agents — but sequentially for desktop tasks
        # (parallel pyautogui would fight over mouse/keyboard)
        for i, task in enumerate(subtasks):
            self._think_log(f"Sub-task {i+1}: {task}")
            result = run_subtask(task, i + 1)
            results[task] = result
            logger.info(f"Sub-agent {i+1} finished: {result[:100]}")

        # Build combined summary
        summaries = []
        for task, result in results.items():
            summaries.append(f"- {task}: {result}")

        combined = f"Completed {len(subtasks)} tasks for '{original_goal}':\n" + "\n".join(summaries)
        self._speak(f"All done. Completed {len(subtasks)} subtasks.")
        return combined

    # ==================================================================
    # Helpers
    # ==================================================================

    def _minimize_own_terminal(self):
        """Minimize the terminal/console window so vision doesn't see it as a blocker."""
        try:
            import pygetwindow as gw
            active = gw.getActiveWindow()
            if active and active.title:
                title_lower = active.title.lower()
                terminal_keywords = [
                    "python", "cmd", "powershell", "terminal",
                    "command prompt", "windowsterminal", "test_agent",
                ]
                if any(kw in title_lower for kw in terminal_keywords):
                    active.minimize()
                    time.sleep(0.5)
                    logger.info(f"Minimized own terminal: {active.title}")
        except Exception as e:
            logger.debug(f"Could not minimize terminal: {e}")

    def _is_stuck(self):
        """Detect if the agent is repeating the same action (stuck in a loop).

        Expanded detection:
        - Same tool twice with error/same result
        - 3 identical consecutive tools
        - Oscillation: 2-tool pattern (A,B,A,B) in last 4 turns
        - Oscillation: 3-tool pattern (A,B,C,A,B,C) in last 6 turns
        - 3+ failures in last 6 turns (cycling through failing tools)
        - Global turn limit exceeded (MAX_AGENT_TURNS)
        """
        # Global turn limit as a safety net
        if self._turn_count >= MAX_AGENT_TURNS:
            return True

        if len(self._history) < 2:
            return False
        last2 = self._history[-2:]
        tools = [h.get("tool", "") for h in last2]
        results = [h.get("result", "") for h in last2]
        # Same tool twice with same or similar result = stuck
        if tools[0] == tools[1] and tools[0]:
            if ("not found" in results[-1].lower()
                    or "error" in results[-1].lower()):
                return True
            if results[0] == results[1]:
                return True
        # 3 turns with same tool = definitely stuck regardless of result
        if len(self._history) >= 3:
            last3_tools = [h.get("tool", "") for h in self._history[-3:]]
            if last3_tools[0] == last3_tools[1] == last3_tools[2] and last3_tools[0]:
                return True

        # Oscillation detection: 2-tool repeating pattern (A,B,A,B)
        if len(self._history) >= 4:
            last4_tools = [h.get("tool", "") for h in self._history[-4:]]
            if (last4_tools[0] and last4_tools[1]
                    and last4_tools[0] != last4_tools[1]
                    and last4_tools[0] == last4_tools[2]
                    and last4_tools[1] == last4_tools[3]):
                return True

        # Oscillation detection: 3-tool repeating pattern (A,B,C,A,B,C)
        if len(self._history) >= 6:
            last6_tools = [h.get("tool", "") for h in self._history[-6:]]
            pattern = last6_tools[:3]
            if (all(t for t in pattern)
                    and len(set(pattern)) >= 2
                    and last6_tools[3:] == pattern):
                return True

        # Expanded: 3+ failures in last 6 turns (even with different tools) = cycling
        if len(self._history) >= 4:
            recent = self._history[-6:]
            failed = [h for h in recent
                      if any(w in h.get("result", "").lower()
                             for w in ["error", "not found", "failed", "blocked", "timeout"])]
            if len(failed) >= 3:
                return True
        return False

    def _get_openclaw_tools_prompt(self):
        """Return OpenClaw tool descriptions for the THINK prompt (if available)."""
        if not self._openclaw_tools:
            return ""
        return (
            "\nADVANCED TOOLS (OpenClaw - use for complex tasks):\n"
            '- openclaw_browser_open: open URL in automated browser. Args: {"url": "https://..."}\n'
            '- openclaw_browser_search: web search. Args: {"query": "search terms"}\n'
            '- openclaw_system_run: run shell command. Args: {"command": "dir"}\n'
            '- openclaw_send_message: send via messaging. Args: {"channel": "whatsapp", "message": "hi"}\n'
        )

    def _speak(self, text):
        """Speak only final results to the user."""
        if self.speak_fn:
            try:
                self.speak_fn(text)
            except Exception:
                pass
        logger.info(f"DesktopAgent: {text}")

    def _think_log(self, text):
        """Print thought to console only (no speech). Agent thinks silently."""
        print(f"  [agent] {text}")
        logger.info(f"DesktopAgent thought: {text}")

    def _speak_action(self, decision):
        """Show what we're about to do in console only (silent thinking)."""
        tool = decision.get("tool", "")
        args = decision.get("args", {})
        reasoning = decision.get("reasoning", "")
        if tool:
            # Show clear action with args
            args_str = ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""
            step_info = ""
            if self._plan_steps and self._current_plan_idx < len(self._plan_steps):
                step_info = f" [Step {self._current_plan_idx + 1}/{len(self._plan_steps)}]"
            desc = f"{tool}({args_str})" if args_str else tool
            why = f" — {reasoning[:60]}" if reasoning else ""
            self._think_log(f"Turn {self._turn_count}{step_info}: {desc}{why}")

    def _announce_progress(self, decision):
        """Brief spoken progress update so user knows what's happening.
        Only announces on significant actions, not every tiny step."""
        tool = decision.get("tool", "")
        args = decision.get("args", {})

        # Map tool actions to short spoken phrases
        _ANNOUNCEMENTS = {
            "open_app": lambda a: f"Opening {a.get('name', 'app')}",
            "close_app": lambda a: f"Closing {a.get('name', 'app')}",
            "search_in_app": lambda a: f"Searching for {a.get('query', 'something')}",
            "google_search": lambda a: f"Searching Google",
            "web_read": lambda a: "Reading the page",
            "create_file": lambda a: "Creating the file",
            "manage_software": lambda a: f"{a.get('action', 'Managing')} software",
            "run_terminal": lambda a: "Running command",
            "manage_files": lambda a: f"{a.get('action', 'Managing')} files",
            "play_music": lambda a: f"Playing {a.get('query', 'music')}",
        }

        announcer = _ANNOUNCEMENTS.get(tool)
        if announcer:
            msg = announcer(args)
            try:
                from speech import speak_async
                speak_async(msg)
            except Exception:
                pass
