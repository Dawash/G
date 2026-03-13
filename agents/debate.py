"""
Debate Agent — Multi-perspective deliberation for critical decisions.

When the swarm faces an ambiguous or high-stakes decision (e.g., "Which approach
to take?", "Is this result correct?", "Should we replan?"), the DebateAgent
spawns multiple viewpoints that argue and vote.

Algorithm:
  1. Frame the question (from orchestrator context)
  2. Generate N perspectives (advocate, skeptic, pragmatist)
  3. Each perspective argues its position (1 LLM call each)
  4. Cross-examination: each perspective responds to others (1 round)
  5. Moderator synthesizes and picks winner (1 LLM call)

Total LLM calls: N + N + 1 = 2N+1 (default N=3 → 7 calls)
Budget-aware: skips debate if LLM budget is low.
"""

import json
import logging
import re
import time

from .base import BaseAgent

logger = logging.getLogger(__name__)

# Perspectives for debate
PERSPECTIVES = [
    {"name": "advocate", "bias": "Argue FOR the proposed approach. Find evidence it will work."},
    {"name": "skeptic", "bias": "Argue AGAINST the proposed approach. Find risks and flaws."},
    {"name": "pragmatist", "bias": "Focus on what's practical and achievable given constraints."},
]

# When to trigger debate
DEBATE_TRIGGERS = {
    "replan": "Should we replan the approach or keep trying?",
    "approach": "Which approach should we take for this goal?",
    "verification": "Did the execution actually achieve the goal?",
    "escalation": "Should we escalate to the user or keep trying autonomously?",
}

MAX_DEBATE_ROUNDS = 2  # Cross-examination rounds
MIN_LLM_BUDGET_FOR_DEBATE = 15  # Skip if fewer than 15 LLM calls remaining

# Maximum LLM calls for the whole swarm (mirrors orchestrator.py constant)
MAX_LLM_CALLS = 40


class DebateAgent(BaseAgent):
    """Multi-perspective deliberation for ambiguous or high-stakes decisions."""

    name = "debate"
    role = "Spawns multiple viewpoints that argue and vote to resolve decisions"

    def run(self, question: str = "", context: dict = None, trigger: str = "approach", **kwargs) -> dict:
        """Run a full debate with cross-examination.

        Args:
            question: The question to debate. If empty, derived from trigger.
            context: Extra context (failed_step, error, progress, actions, goal).
            trigger: One of DEBATE_TRIGGERS keys.

        Returns:
            {"status": "ok", "winner": str, "reasoning": str,
             "confidence": float, "positions": list}
        """
        self._log(f"Debate triggered: {trigger}")
        context = context or {}

        # --- Budget check: skip if LLM budget is low ---
        total_calls = self.bb.get("total_llm_calls", 0)
        remaining = MAX_LLM_CALLS - total_calls
        if remaining < MIN_LLM_BUDGET_FOR_DEBATE:
            self._log(f"Budget too low ({remaining} remaining), skipping debate")
            return self._conservative_default(trigger, context)

        # --- Build the debate question ---
        if not question:
            question = DEBATE_TRIGGERS.get(trigger, f"What should we do? (trigger={trigger})")

        # --- Build context string from blackboard ---
        ctx_str = self._build_context_string(context)

        # --- Phase 1: Each perspective argues its position ---
        positions = []
        for perspective in PERSPECTIVES:
            argument = self._generate_argument(perspective, question, ctx_str)
            positions.append({
                "name": perspective["name"],
                "argument": argument,
                "rebuttal": "",
            })

        # --- Phase 2: Cross-examination (1 round) ---
        # Each perspective responds to the other two arguments
        if remaining >= MIN_LLM_BUDGET_FOR_DEBATE + len(PERSPECTIVES):
            for i, pos in enumerate(positions):
                others = [p for j, p in enumerate(positions) if j != i]
                rebuttal = self._cross_examine(pos, others, question, ctx_str)
                positions[i]["rebuttal"] = rebuttal

        # --- Phase 3: Moderator synthesizes and picks winner ---
        result = self._moderate(question, positions, trigger, ctx_str)

        # --- Log to blackboard ---
        debate_record = {
            "trigger": trigger,
            "question": question,
            "winner": result["winner"],
            "confidence": result["confidence"],
            "reasoning": result["reasoning"],
            "positions": [
                {"name": p["name"], "argument": p["argument"][:200], "rebuttal": p["rebuttal"][:200]}
                for p in positions
            ],
            "timestamp": time.time(),
        }
        self.bb.append("debates", debate_record)
        self._post({"debate": trigger, "winner": result["winner"]}, msg_type="result")
        self._log(f"Debate result: winner={result['winner']}, confidence={result['confidence']:.2f}")

        return result

    def quick_debate(self, question: str, options: list) -> str:
        """Lightweight debate: 3 LLM calls (1 per perspective), no cross-examination.

        Args:
            question: The question to decide.
            options: List of option strings (e.g., ["continue", "research", "replan"]).

        Returns:
            The winning option string.
        """
        # Budget check
        total_calls = self.bb.get("total_llm_calls", 0)
        remaining = MAX_LLM_CALLS - total_calls
        if remaining < len(PERSPECTIVES) + 1:
            self._log(f"Budget too low for quick debate ({remaining} remaining)")
            # Default to the middle/safest option
            return options[len(options) // 2] if options else "continue"

        ctx_str = self._build_context_string({})
        options_str = ", ".join(f'"{o}"' for o in options)

        votes = {}
        for perspective in PERSPECTIVES:
            prompt = (
                f"You are the {perspective['name']}.\n"
                f"Bias: {perspective['bias']}\n\n"
                f"Context:\n{ctx_str}\n\n"
                f"Question: {question}\n"
                f"Options: {options_str}\n\n"
                f"Pick ONE option and explain briefly why.\n"
                f"Return JSON: {{\"vote\": \"option\", \"reason\": \"why\"}}\n"
                f"JSON only."
            )
            raw = self._llm_call(prompt)
            vote = self._parse_vote(raw, options)
            if vote:
                votes[perspective["name"]] = vote
                self._log(f"  {perspective['name']} votes: {vote}")

        # Tally votes
        if not votes:
            return options[0] if options else "continue"

        vote_counts = {}
        for v in votes.values():
            vote_counts[v] = vote_counts.get(v, 0) + 1

        winner = max(vote_counts, key=vote_counts.get)

        # Log
        self.bb.append("debates", {
            "type": "quick",
            "question": question,
            "votes": votes,
            "winner": winner,
            "timestamp": time.time(),
        })

        self._log(f"Quick debate: {votes} -> winner={winner}")
        return winner

    # --- Internal Methods ---

    def _build_context_string(self, context: dict) -> str:
        """Build a context string from blackboard + provided context."""
        parts = []

        goal = context.get("goal") or self.bb.get("goal", "")
        if goal:
            parts.append(f"Goal: {goal}")

        progress = context.get("progress") or self.bb.get_plan_progress()
        if progress and progress.get("total", 0) > 0:
            parts.append(f"Progress: {progress['done']}/{progress['total']} steps "
                         f"({progress.get('pct', 0)}% done, {progress.get('failed', 0)} failed)")

        if context.get("failed_step"):
            parts.append(f"Failed step: {context['failed_step']}")

        if context.get("error"):
            parts.append(f"Last error: {str(context['error'])[:200]}")

        # Recent actions from blackboard
        recent = self.bb.get_recent_actions(5)
        if recent:
            action_lines = []
            for a in recent[-5:]:
                status = "OK" if a["success"] else "FAIL"
                action_lines.append(f"  {a['tool']}() -> {status}: {a['result'][:60]}")
            parts.append(f"Recent actions:\n" + "\n".join(action_lines))

        # Reflexions
        reflexions = self.bb.get("reflexions", [])
        if reflexions:
            parts.append(f"Lessons learned: {'; '.join(str(r)[:80] for r in reflexions[-3:])}")

        if context.get("actions"):
            successes = context["actions"]
            if isinstance(successes, list) and successes:
                parts.append(f"Successful actions: {len(successes)}")

        return "\n".join(parts) if parts else "No context available."

    def _generate_argument(self, perspective: dict, question: str, context: str) -> str:
        """Generate one perspective's argument via LLM call."""
        prompt = (
            f"You are the {perspective['name']} in a decision-making debate.\n"
            f"Your role: {perspective['bias']}\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Make your argument in 2-3 concise sentences. Be specific, cite evidence "
            f"from the context. End with your recommendation.\n"
            f"Response (plain text, no JSON):"
        )
        return self._llm_call(prompt).strip()

    def _cross_examine(self, position: dict, others: list, question: str, context: str) -> str:
        """One perspective responds to others' arguments."""
        others_text = "\n".join(
            f"  {o['name']}: {o['argument'][:200]}"
            for o in others
        )
        prompt = (
            f"You are the {position['name']}. You argued:\n"
            f"\"{position['argument'][:200]}\"\n\n"
            f"Other perspectives:\n{others_text}\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Respond to the other arguments in 2 sentences. Point out what they "
            f"miss or where you agree. Keep your final recommendation.\n"
            f"Response (plain text):"
        )
        return self._llm_call(prompt).strip()

    def _moderate(self, question: str, positions: list, trigger: str, context: str) -> dict:
        """Moderator synthesizes all arguments and picks a winner."""
        debate_text = ""
        for p in positions:
            debate_text += f"\n--- {p['name'].upper()} ---\n"
            debate_text += f"Argument: {p['argument'][:250]}\n"
            if p["rebuttal"]:
                debate_text += f"Rebuttal: {p['rebuttal'][:250]}\n"

        # Determine valid options based on trigger
        if trigger == "replan":
            options_text = '"continue" (keep trying current plan) or "replan" (create new plan)'
        elif trigger == "verification":
            options_text = '"done" (goal achieved) or "continue" (not done yet)'
        elif trigger == "escalation":
            options_text = '"continue" (keep trying) or "escalate" (ask user)'
        else:
            options_text = 'the best approach as a short label'

        prompt = (
            f"You are a neutral moderator deciding the outcome of a debate.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Debate:\n{debate_text}\n\n"
            f"Pick the winner: {options_text}\n\n"
            f"Return JSON:\n"
            f'{{"winner": "chosen_option", "reasoning": "why this wins", '
            f'"confidence": 0.8}}\n'
            f"confidence is 0.0 to 1.0 (how certain you are).\n"
            f"JSON only."
        )
        raw = self._llm_call(prompt)
        return self._parse_moderator(raw, trigger)

    def _parse_moderator(self, raw: str, trigger: str) -> dict:
        """Parse moderator's JSON response with fallback."""
        default = self._default_for_trigger(trigger)
        if not raw:
            return default

        try:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                winner = str(data.get("winner", default["winner"])).strip().lower()
                reasoning = str(data.get("reasoning", "No reasoning provided"))
                confidence = float(data.get("confidence", 0.5))
                confidence = max(0.0, min(1.0, confidence))
                return {
                    "status": "ok",
                    "winner": winner,
                    "reasoning": reasoning,
                    "confidence": confidence,
                    "positions": [],
                }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"[debate] Failed to parse moderator JSON: {e}")

        # Regex fallback: look for winner keyword in raw text
        raw_lower = raw.lower()
        if trigger == "replan":
            if "replan" in raw_lower:
                return {**default, "winner": "replan", "reasoning": raw[:200]}
            return {**default, "winner": "continue", "reasoning": raw[:200]}
        elif trigger == "verification":
            if "done" in raw_lower and "not done" not in raw_lower:
                return {**default, "winner": "done", "confidence": 0.6, "reasoning": raw[:200]}
            return {**default, "winner": "continue", "reasoning": raw[:200]}

        return default

    def _parse_vote(self, raw: str, options: list) -> str:
        """Parse a vote from LLM response, matching against valid options."""
        if not raw:
            return ""

        # Try JSON parse
        try:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
                vote = str(data.get("vote", "")).strip().lower()
                if vote in [o.lower() for o in options]:
                    # Return the original-cased option
                    for o in options:
                        if o.lower() == vote:
                            return o
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: find first option mentioned in raw text
        raw_lower = raw.lower()
        for o in options:
            if o.lower() in raw_lower:
                return o

        return ""

    def _conservative_default(self, trigger: str, context: dict) -> dict:
        """Return a conservative default when budget is too low for debate."""
        defaults = {
            "replan": {"winner": "continue", "reasoning": "Budget too low for debate, continuing current plan", "confidence": 0.3},
            "approach": {"winner": "default", "reasoning": "Budget too low, using default approach", "confidence": 0.3},
            "verification": {"winner": "continue", "reasoning": "Budget too low, cannot verify, continuing", "confidence": 0.3},
            "escalation": {"winner": "continue", "reasoning": "Budget too low, continuing autonomously", "confidence": 0.3},
        }
        result = defaults.get(trigger, {"winner": "continue", "reasoning": "Budget too low", "confidence": 0.3})
        return {"status": "ok", "positions": [], **result}

    def _default_for_trigger(self, trigger: str) -> dict:
        """Default result when LLM parsing fails."""
        defaults = {
            "replan": {"winner": "continue", "confidence": 0.4},
            "approach": {"winner": "default", "confidence": 0.4},
            "verification": {"winner": "continue", "confidence": 0.4},
            "escalation": {"winner": "continue", "confidence": 0.4},
        }
        base = defaults.get(trigger, {"winner": "continue", "confidence": 0.4})
        return {"status": "ok", "reasoning": "Could not parse moderator response", "positions": [], **base}
