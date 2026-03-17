"""
Critic Agent — Verification + scoring after execution.

Runs after every N actions (configurable) to:
  1. Assess whether recent actions actually succeeded
  2. Score progress toward the goal (0-100)
  3. Detect if the executor is stuck or going in circles
  4. Decide: continue / retry / replan / escalate to researcher

Uses self-consistency: generates 2 independent assessments and
takes the more conservative score (safety-first).
"""

import json
import logging
import re
import time

from .base import BaseAgent

logger = logging.getLogger(__name__)

# Run critic every N actions
CRITIC_INTERVAL = 3
# Score threshold to continue without intervention
SCORE_CONTINUE = 70
# Score threshold to trigger researcher
SCORE_RESEARCH = 40
# Score threshold to force replan
SCORE_REPLAN = 25


class CriticAgent(BaseAgent):
    """Evaluates execution progress and decides next course of action."""

    name = "critic"
    role = "Quality assessor that scores progress and detects failures"

    def run(self, **kwargs) -> dict:
        """Evaluate recent execution progress.

        Returns:
            {"status": "ok", "score": int, "verdict": str, "reason": str}
            verdict: "continue" | "retry" | "research" | "replan" | "done" | "abort"
        """
        self._log("Evaluating progress...")
        self.bb.set("phase", "critiquing")

        goal = self.bb.get("goal", "")
        progress = self.bb.get_plan_progress()
        recent = self.bb.get_recent_actions(CRITIC_INTERVAL * 2)
        plan = self.bb.get("plan", [])

        if not recent:
            return {"status": "ok", "score": 50, "verdict": "continue",
                    "reason": "No actions to evaluate yet"}

        # --- Check for completion ---
        if progress["pending"] == 0 and progress["done"] > 0:
            score = self._assess_completion(goal, recent)
            if score >= SCORE_CONTINUE:
                return {"status": "ok", "score": score, "verdict": "done",
                        "reason": "All steps completed successfully"}

        # --- Stuck detection ---
        stuck = self._detect_stuck(recent)
        if stuck:
            return {"status": "ok", "score": 20, "verdict": "replan",
                    "reason": f"Stuck: {stuck}"}

        # --- LLM Assessment (self-consistency: decoupled views) ---
        # Optimistic: sees tool outputs + raw results only (no plan status)
        # Critical:   sees goal text + failure patterns only (no tool history)
        # Two different information views → genuinely independent assessments
        score1, reason1 = self._assess(goal, recent, plan, perspective="optimistic")
        score2, reason2 = self._assess(goal, recent, plan, perspective="critical")

        # Take the MORE CONSERVATIVE score (safety-first) — do NOT average.
        # Averaging was negating the safety-first invariant: if one view says 30
        # and the other says 80, averaging gives 55 which is too optimistic.
        if score1 <= score2:
            final_score, reason = score1, reason1
        else:
            final_score, reason = score2, reason2

        # --- Decide verdict ---
        verdict = self._decide_verdict(final_score, progress)

        # Store score
        current = self.bb.get_current_step()
        step_id = current.id if current else "unknown"
        self.bb.append("critic_scores", {
            "step_id": step_id,
            "score": final_score,
            "reason": reason,
            "verdict": verdict,
            "timestamp": time.time(),
        })

        self._post({"score": final_score, "verdict": verdict, "reason": reason}, msg_type="result")
        self._log(f"Score: {final_score}/100, Verdict: {verdict}")

        return {"status": "ok", "score": final_score, "verdict": verdict, "reason": reason}

    def quick_check(self, step_result: str, step_desc: str) -> dict:
        """Fast check after a single step (no LLM, pattern-based).

        Returns: {"ok": bool, "issue": str}
        """
        lower = step_result.lower()

        # Clear failures
        fail_patterns = [
            "error:", "failed:", "not found", "timed out", "timeout",
            "permission denied", "access denied", "could not",
            "blocked", "captcha", "rate limit",
        ]
        for p in fail_patterns:
            if p in lower:
                return {"ok": False, "issue": f"Step failed: {p} detected in result"}

        # Empty result
        if not step_result.strip():
            return {"ok": False, "issue": "Empty result from step execution"}

        # Looks OK
        return {"ok": True, "issue": ""}

    def _assess(self, goal: str, recent: list, plan: list,
                perspective: str = "critical") -> tuple:
        """Single LLM assessment using a specific information view.

        Optimistic view: sees tool names + raw outputs ONLY — no plan status.
          Measures: "did the tools actually do something useful?"
        Critical view: sees goal + failure count + error text ONLY — no plan.
          Measures: "does the evidence of actual outcomes match the goal?"

        Two different grounding sets → genuinely independent assessments.
        """
        if perspective == "optimistic":
            # Tool-output view: what did the tools produce?
            action_summary = "\n".join(
                f"  {i+1}. {a['tool']} returned: {a['result'][:100]}"
                for i, a in enumerate(recent[-6:])
            )
            prompt = (
                f"Desktop automation goal: \"{goal}\"\n\n"
                f"Tool outputs (last {min(6, len(recent))} actions):\n{action_summary}\n\n"
                f"Based ONLY on the tool output text above (ignore plan status), "
                f"how much of the goal has been achieved?\n"
                f"Rate 0-100. 100 means the goal is fully accomplished.\n"
                f"Return JSON: {{\"score\": 75, \"reason\": \"evidence from tool outputs\"}}\n"
                f"JSON only."
            )
        else:
            # Failure-pattern view: what went wrong?
            failures = [a for a in recent if not a["success"]]
            errors = "; ".join(a["result"][:60] for a in failures[-3:]) or "none"
            fail_rate = round(len(failures) / max(len(recent), 1), 2)
            prompt = (
                f"Desktop automation goal: \"{goal}\"\n\n"
                f"Execution statistics:\n"
                f"  Total actions: {len(recent)}\n"
                f"  Failures: {len(failures)} ({fail_rate*100:.0f}%)\n"
                f"  Recent errors: {errors}\n\n"
                f"Be strict — only count fully verified successes toward the goal.\n"
                f"A high failure rate strongly suggests the goal is NOT achieved.\n"
                f"Rate 0-100. Return JSON: {{\"score\": 40, \"reason\": \"failure analysis\"}}\n"
                f"JSON only."
            )

        raw = self._llm_call(prompt)
        try:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                score = int(data.get("score", 50))
                score = max(0, min(100, score))
                return (score, data.get("reason", "No reason given"))
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: estimate from success rate
        if recent:
            success_rate = sum(1 for a in recent if a["success"]) / len(recent)
            return (int(success_rate * 80), "Estimated from success rate")
        return (50, "Could not assess")

    def _detect_stuck(self, recent: list) -> str:
        """Detect if executor is looping or stuck."""
        if len(recent) < 4:
            return ""

        # Check for repeated identical actions
        last_4 = [(a["tool"], json.dumps(a["args"], sort_keys=True)) for a in recent[-4:]]
        if last_4[0] == last_4[2] and last_4[1] == last_4[3]:
            return f"Oscillating between {last_4[0][0]} and {last_4[1][0]}"

        # Check for all failures in a row
        if all(not a["success"] for a in recent[-3:]):
            return f"3 consecutive failures: {recent[-1]['tool']}"

        # Check for repeated same tool with same args
        if len(set(last_4)) == 1:
            return f"Same action repeated 4 times: {last_4[0][0]}"

        return ""

    def _decide_verdict(self, score: int, progress: dict) -> str:
        """Decide what to do based on score."""
        if score >= 90 and progress["pending"] == 0:
            return "done"
        if score >= SCORE_CONTINUE:
            return "continue"
        if score >= SCORE_RESEARCH:
            return "research"
        if score >= SCORE_REPLAN:
            return "replan"
        return "abort"

    def _assess_completion(self, goal: str, recent: list) -> int:
        """Completion check: success rate + keyword evidence in results."""
        success_count = sum(1 for a in recent if a["success"])
        if success_count == 0:
            return 30
        base = min(90, 55 + (success_count / max(len(recent), 1)) * 35)

        # Bonus: check if goal keywords appear in any tool result
        goal_words = set(w.lower() for w in goal.split() if len(w) > 3)
        all_results = " ".join(a.get("result", "") for a in recent[-4:]).lower()
        keyword_hits = sum(1 for w in goal_words if w in all_results)
        if keyword_hits >= 2:
            base = min(95, base + 10)  # Evidence bonus
        elif keyword_hits == 0 and success_count < len(recent):
            base = max(30, base - 15)  # No evidence penalty

        return int(base)


def _brief_args(args: dict) -> str:
    if not args:
        return ""
    parts = []
    for k, v in list(args.items())[:3]:
        sv = str(v)[:25]
        parts.append(f"{k}={sv}")
    return ", ".join(parts)
