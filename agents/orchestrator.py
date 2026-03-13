"""
SwarmOrchestrator — State machine that coordinates the multi-agent team.

Flow:
  PLAN → EXECUTE → CRITIQUE → (continue | RESEARCH | REPLAN) → EXECUTE → ...
  → DONE → LEARN

State transitions:
  idle → planning → executing → critiquing
  critiquing → executing (continue)
  critiquing → researching (low score)
  critiquing → planning (replan needed)
  critiquing → done (goal achieved)
  critiquing → aborting (unrecoverable)
  researching → executing (got fix)
  done → learning → idle

This is a simple, zero-dependency state machine — no LangGraph needed.
Works with any LLM size (7B-72B via Ollama).
"""

import logging
import time

from .blackboard import Blackboard
from .planner import PlannerAgent
from .executor import ExecutorAgent
from .critic import CriticAgent, CRITIC_INTERVAL, SCORE_CONTINUE, SCORE_RESEARCH
from .researcher import ResearcherAgent
from .memory_agent import MemoryAgent
from .debate import DebateAgent

logger = logging.getLogger(__name__)

# Max total actions before forced abort (very high — let agents work until done)
MAX_TOTAL_ACTIONS = 999
# Max total LLM calls (very high — no artificial limit)
MAX_LLM_CALLS = 999
# Max replans before abort (generous — allow many retries)
MAX_REPLANS = 20
# Max total time (seconds) — 1 hour
MAX_TOTAL_TIME = 3600


class SwarmOrchestrator:
    """Coordinates the 5-agent team to accomplish complex goals.

    Usage:
        orch = SwarmOrchestrator(brain)
        result = orch.execute("Book a flight to Tokyo and create itinerary PDF")
    """

    def __init__(self, brain, speak_fn=None):
        """
        Args:
            brain: Brain instance (provides LLM, tools, action_registry).
            speak_fn: Optional TTS callback for status updates.
        """
        self.brain = brain
        self.speak_fn = speak_fn
        self._bb = Blackboard()
        self._replan_count = 0
        self._actions_since_critique = 0

        # Initialize agents with shared blackboard
        llm_fn = brain.quick_chat
        self._planner = PlannerAgent(llm_fn, self._bb)
        self._executor = ExecutorAgent(
            llm_fn, self._bb,
            brain=brain,
            action_registry=brain.action_registry,
            speak_fn=speak_fn,
        )
        self._critic = CriticAgent(llm_fn, self._bb)
        self._researcher = ResearcherAgent(llm_fn, self._bb)
        self._memory = MemoryAgent(llm_fn, self._bb)
        self._debate = DebateAgent(llm_fn, self._bb)

    def execute(self, goal: str) -> str:
        """Execute a complex goal using the multi-agent team.

        Returns: Final result string (human-readable).
        """
        logger.info(f"[Swarm] Starting: {goal[:80]}")
        t0 = time.perf_counter()

        # Initialize blackboard
        self._bb.set("goal", goal)
        self._bb.set("start_time", time.time())
        self._bb.set("phase", "idle")

        # --- Load past reflexions for this type of goal ---
        self._load_reflexions(goal)

        try:
            result = self._run_state_machine(goal)
        except Exception as e:
            logger.error(f"[Swarm] Fatal error: {e}")
            result = f"Error: {e}"

        elapsed = time.perf_counter() - t0
        logger.info(f"[Swarm] Completed in {elapsed:.1f}s: {result[:100]}")

        # --- Learn from outcome ---
        success = "error" not in result.lower()[:50] and "abort" not in result.lower()
        try:
            self._memory.run(goal=goal, success=success)
        except Exception as e:
            logger.warning(f"[Swarm] Memory agent failed: {e}")

        # --- Stats ---
        stats = self._get_stats()
        logger.info(f"[Swarm] Stats: {stats}")

        return result

    def _run_state_machine(self, goal: str) -> str:
        """Main state machine loop."""

        # ========== PHASE 1: PLAN ==========
        plan_result = self._planner.run(
            goal=goal,
            available_tools=self._get_tool_names(),
        )
        if plan_result["status"] == "error":
            return f"Planning failed: {plan_result.get('result', 'unknown error')}"

        plan = plan_result["plan"]
        if not plan:
            return "Could not create a plan for this goal."

        logger.info(f"[Swarm] Plan: {len(plan)} steps, approach: {plan_result.get('approach', '?')}")
        if self.speak_fn:
            try:
                self.speak_fn(f"Working on it... {len(plan)} steps planned.")
            except Exception:
                pass

        # ========== PHASE 2: EXECUTE + CRITIQUE LOOP ==========
        self._actions_since_critique = 0

        while True:
            # --- Cancellation check (set by agent_runner on user interrupt) ---
            if self._bb.get("cancelled", False):
                logger.info("[Swarm] Cancelled by user")
                return self._build_partial_result(goal, "Cancelled by user")

            # --- Budget checks ---
            if self._bb.get("total_tool_calls", 0) >= MAX_TOTAL_ACTIONS:
                logger.warning("[Swarm] Max actions reached, aborting")
                return self._build_partial_result(goal, "Reached maximum action limit")

            if self._bb.get("total_llm_calls", 0) >= MAX_LLM_CALLS:
                logger.warning("[Swarm] Max LLM calls reached, aborting")
                return self._build_partial_result(goal, "Reached LLM call budget")

            elapsed = time.time() - self._bb.get("start_time", time.time())
            if elapsed > MAX_TOTAL_TIME:
                logger.warning("[Swarm] Timeout, aborting")
                return self._build_partial_result(goal, "Timed out")

            # --- Get next step ---
            ready_steps = self._bb.get_ready_steps()
            if not ready_steps:
                # Check if done or all failed
                progress = self._bb.get_plan_progress()
                if progress["done"] > 0 and progress["pending"] == 0:
                    return self._build_success_result(goal)
                if progress["failed"] > 0 and progress["pending"] == 0:
                    return self._build_partial_result(goal, "Some steps failed")
                # Nothing ready but steps remain (stuck)
                return self._build_partial_result(goal, "No executable steps remaining")

            step = ready_steps[0]

            # --- Execute step ---
            exec_result = self._executor.run(step=step)
            self._actions_since_critique += 1

            # Quick check after each action
            if exec_result["status"] == "takeover":
                return f"Paused for user action: {step.description}"

            if exec_result["status"] == "failed":
                # Try researcher immediately on failure
                research = self._researcher.run(
                    failed_step=step.description,
                    error=exec_result.get("result", ""),
                    goal=goal,
                )
                if research.get("solution"):
                    # Apply fix and retry
                    self._bb.append("reflexions", research["solution"])
                    # Retry with updated knowledge
                    retry = self._executor.run(step=step)
                    if retry["status"] == "ok":
                        self._actions_since_critique += 1
                    else:
                        # Mark failed and advance
                        self._bb.mark_step(step.id, "failed", result=exec_result.get("result", ""))
                        self._bb.advance_step()
                        continue
                else:
                    self._bb.advance_step()
                    continue

            # --- Periodic critique ---
            if self._actions_since_critique >= CRITIC_INTERVAL:
                self._actions_since_critique = 0
                self._bb.checkpoint()

                critique = self._critic.run()
                verdict = critique.get("verdict", "continue")
                score = critique.get("score", 50)

                logger.info(f"[Swarm] Critic: {verdict} (score={score})")

                # When critic is uncertain (score between SCORE_RESEARCH and SCORE_CONTINUE),
                # debate to break the tie
                if SCORE_RESEARCH <= score < SCORE_CONTINUE:
                    debate_verdict = self._debate.quick_debate(
                        f"The critic scored progress at {score}/100. Should we continue, research, or replan?",
                        options=["continue", "research", "replan"]
                    )
                    logger.info(f"[Swarm] Debate overrides ambiguous verdict: {verdict} -> {debate_verdict}")
                    verdict = debate_verdict

                if verdict == "done":
                    # Verify completion via debate before declaring done
                    actions = self._bb.get("action_history", [])
                    successes = [a for a in actions if a["success"]]
                    debate_result = self._debate.run(trigger="verification", context={
                        "actions": successes,
                        "goal": goal,
                    })
                    if debate_result.get("confidence", 0) < 0.6:
                        logger.info(f"[Swarm] Debate not confident goal is done "
                                    f"(confidence={debate_result.get('confidence', 0):.2f}), continuing")
                        continue
                    return self._build_success_result(goal)

                elif verdict == "replan":
                    if self._replan_count >= MAX_REPLANS:
                        return self._build_partial_result(goal, "Max replans exceeded")

                    # Debate whether to replan or keep trying
                    current = self._bb.get_current_step()
                    failed_desc = current.description if current else "unknown"
                    last_error = self._bb.get("errors", [{}])[-1].get("error", "unknown") if self._bb.get("errors") else "unknown"
                    progress = self._bb.get_plan_progress()

                    debate_result = self._debate.run(trigger="replan", context={
                        "failed_step": failed_desc,
                        "error": last_error,
                        "progress": progress,
                        "goal": goal,
                    })
                    if debate_result.get("winner") == "continue":
                        logger.info("[Swarm] Debate says continue, skipping replan")
                        continue

                    self._replan_count += 1
                    replan_result = self._planner.replan(goal, failed_desc, last_error)
                    if replan_result["status"] != "ok":
                        return self._build_partial_result(goal, "Replan failed")
                    continue

                elif verdict == "research":
                    current = self._bb.get_current_step()
                    step_desc = current.description if current else goal
                    last_error = self._bb.get("errors", [{}])[-1].get("error", "") if self._bb.get("errors") else ""
                    self._researcher.run(failed_step=step_desc, error=last_error, goal=goal)
                    continue

                elif verdict == "abort":
                    return self._build_partial_result(goal, f"Aborted by critic (score={score})")

                # "continue" — keep going

            # Advance to next step
            self._bb.advance_step()

        # Shouldn't reach here
        return self._build_partial_result(goal, "Unexpected loop exit")

    # --- Result Building ---

    def _build_success_result(self, goal: str) -> str:
        """Build human-readable success message."""
        progress = self._bb.get_plan_progress()
        actions = self._bb.get("action_history", [])
        successes = [a for a in actions if a["success"]]

        if successes:
            last_result = successes[-1].get("result", "")
            if last_result and len(last_result) > 10:
                return last_result

        return f"Done! Completed {progress['done']} steps for: {goal}"

    def _build_partial_result(self, goal: str, reason: str) -> str:
        """Build message for partial completion."""
        progress = self._bb.get_plan_progress()
        actions = self._bb.get("action_history", [])
        successes = [a for a in actions if a["success"]]

        parts = [f"Partially completed ({progress['done']}/{progress['total']} steps)."]
        if reason:
            parts.append(f"Stopped because: {reason}")
        if successes:
            parts.append(f"Last successful action: {successes[-1]['tool']}({_brief(successes[-1]['args'])})")

        return " ".join(parts)

    # --- Helpers ---

    def _get_tool_names(self) -> list:
        """Get available tool names from brain."""
        try:
            from tools.registry import get_default
            registry = get_default()
            if registry:
                return list(registry.all_names())
        except Exception:
            pass
        return ["open_app", "close_app", "google_search", "browser_action",
                "click_at", "type_text", "press_key", "take_screenshot",
                "run_terminal", "create_file", "get_weather", "set_reminder",
                "play_music", "web_read", "send_email"]

    def _load_reflexions(self, goal: str):
        """Load past reflexions relevant to this goal."""
        results = self._bb.search_similar(goal, top_k=3)
        for r in results:
            if r["similarity"] > 0.4:
                self._bb.append("reflexions", r["text"])

        # Also check skill library reflexions
        try:
            from skills import SkillLibrary
            lib = SkillLibrary()
            matches = lib.find_skill(goal, min_similarity=0.5, limit=1)
            if matches:
                skill = matches[0]
                # Reflexions are stored in skill
                if skill.get("reflexions"):
                    for ref in skill["reflexions"][:3]:
                        self._bb.append("reflexions", str(ref))
        except Exception:
            pass

    def _get_stats(self) -> dict:
        """Gather execution statistics."""
        return {
            "total_actions": self._bb.get("total_tool_calls", 0),
            "total_llm_calls": self._bb.get("total_llm_calls", 0),
            "replans": self._replan_count,
            "plan_progress": self._bb.get_plan_progress(),
            "errors": len(self._bb.get("errors", [])),
            "reflexions": len(self._bb.get("reflexions", [])),
        }


def _brief(args: dict) -> str:
    if not args:
        return ""
    return ", ".join(f"{k}={str(v)[:20]}" for k, v in list(args.items())[:2])
