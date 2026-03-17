"""
Memory Agent — Skill evolution + reflexion learning.

Runs after task completion (success or failure) to:
  1. Save successful execution paths as reusable skills
  2. Store failure reflexions for future avoidance
  3. Update tool reliability scores
  4. Detect recurring patterns and suggest improvements

Upgrades from ExperienceLearner + SkillLibrary:
  - Self-consistency reflexion (generate 3, vote on best)
  - Cross-session pattern detection
  - Skill refinement (update existing skills with better sequences)
"""

import json
import logging
import threading
import time

from .base import BaseAgent

logger = logging.getLogger(__name__)


class MemoryAgent(BaseAgent):
    """Learns from execution outcomes and evolves the skill library."""

    name = "memory"
    role = "Learning agent that stores skills and reflexions"

    def run(self, goal: str = "", success: bool = True, **kwargs) -> dict:
        """Process execution outcome and learn.

        Args:
            goal: The completed goal.
            success: Whether the goal was achieved.

        Returns:
            {"status": "ok", "skills_saved": int, "reflexions": int}
        """
        goal = goal or self.bb.get("goal", "")
        self._log(f"Learning from {'success' if success else 'failure'}: {goal[:60]}")

        skills_saved = 0
        reflexions_stored = 0

        if success:
            skills_saved = self._save_skill(goal)
            if skills_saved:
                # Background: improve the newly saved skill with thinking LLM
                threading.Thread(
                    target=self._bg_improve_skill, args=(goal,), daemon=True
                ).start()
        else:
            reflexions_stored = self._store_reflexions(goal)

        # Always update tool reliability
        self._update_tool_memory()

        # Check for recurring patterns
        self._detect_patterns()

        self._post({
            "skills_saved": skills_saved,
            "reflexions": reflexions_stored,
            "success": success,
        }, msg_type="result")

        return {
            "status": "ok",
            "skills_saved": skills_saved,
            "reflexions": reflexions_stored,
        }

    def _save_skill(self, goal: str) -> int:
        """Save successful execution as a reusable skill."""
        actions = self.bb.get("action_history", [])
        if len(actions) < 2:
            return 0  # Too simple to save

        # Build tool sequence from actions
        tool_sequence = []
        for a in actions:
            if a["success"]:
                tool_sequence.append({
                    "tool": a["tool"],
                    "args": a["args"],
                    "description": f"{a['tool']}({_brief(a['args'])})",
                })

        if not tool_sequence:
            return 0

        try:
            from skills import SkillLibrary
            lib = SkillLibrary()

            # Check if similar skill exists — refine instead of duplicate
            existing = lib.find_skill(goal, min_similarity=0.85, limit=1)
            if existing and existing[0].get("similarity", 0) >= 0.85:
                # Refine existing skill
                skill_name = existing[0].get("name", "")
                if skill_name:
                    lib.refine_skill(skill_name, tool_sequence,
                                     f"Refined from successful execution on {time.strftime('%Y-%m-%d')}")
                    self._log(f"Refined existing skill: {skill_name}")
                    return 1

            # Save new skill
            import re
            name = re.sub(r'[^\w\s]', '', goal.lower())[:50].strip().replace(' ', '_')
            name = f"skill_{name}_{int(time.time()) % 10000}"

            lib.save_skill(
                name=name,
                description=goal,
                goal=goal,
                tool_sequence=tool_sequence,
                tags=list({a["tool"] for a in tool_sequence}),
                duration=self.bb.get("action_history", [{}])[-1].get("timestamp", 0) - self.bb.get("start_time", 0),
            )
            self._log(f"Saved new skill: {name} ({len(tool_sequence)} steps)")
            return 1

        except Exception as e:
            logger.warning(f"Failed to save skill: {e}")
            return 0

    def _store_reflexions(self, goal: str) -> int:
        """Generate and store failure reflexions with self-consistency."""
        actions = self.bb.get("action_history", [])
        errors = self.bb.get("errors", [])

        if not errors:
            return 0

        error_summary = "; ".join(
            f"{e.get('step', '?')}: {e.get('error', '')[:60]}" for e in errors[-5:]
        )
        action_summary = "\n".join(
            f"  {a['tool']}({_brief(a['args'])}) -> {'OK' if a['success'] else 'FAIL'}"
            for a in actions[-8:]
        )

        # Generate multiple reflexions (self-consistency)
        reflexions = []
        for i in range(2):
            prompt = (
                f"Analyze this failed automation task and explain what went wrong.\n\n"
                f"Goal: {goal}\n"
                f"Actions taken:\n{action_summary}\n"
                f"Errors: {error_summary}\n\n"
                f"In 1-2 sentences, explain:\n"
                f"1. The root cause of failure\n"
                f"2. What should be done differently next time\n"
                f"Be specific — mention exact tools, strategies, or approaches."
            )
            result = self._llm_call(prompt)
            if result:
                reflexions.append(result.strip())

        if not reflexions:
            return 0

        # Pick best reflexion (longest = most detailed, simple heuristic)
        best = max(reflexions, key=len)
        self.bb.append("reflexions", best)

        # Store in blackboard vector memory
        self.bb.index_document(f"reflexion_{int(time.time())}", f"FAILURE: {goal} -> {best}")

        # Store in skill library reflexions
        try:
            from skills import SkillLibrary
            lib = SkillLibrary()
            # Find any related skill
            related = lib.find_skill(goal, min_similarity=0.5, limit=1)
            if related:
                lib.add_reflection(related[0].get("name", ""), best)
        except Exception:
            pass

        # Store in cognitive engine
        try:
            from cognitive import ExperienceLearner
            learner = ExperienceLearner()
            for a in actions:
                learner.log_outcome(
                    goal, a["tool"], a["args"], a["success"], a.get("result", "")
                )
        except Exception:
            pass

        self._log(f"Stored {len(reflexions)} reflexions, best: {best[:80]}")
        return len(reflexions)

    def _update_tool_memory(self):
        """Update tool reliability scores from action history."""
        actions = self.bb.get("action_history", [])
        if not actions:
            return

        try:
            from cognitive import ExperienceLearner
            learner = ExperienceLearner()
            goal = self.bb.get("goal", "")
            for a in actions:
                learner.log_outcome(
                    goal, a["tool"], a["args"], a["success"], a.get("result", "")[:200]
                )
        except Exception:
            pass

    def _detect_patterns(self):
        """Detect recurring failure/success patterns across sessions."""
        try:
            from cognitive import ExperienceLearner
            learner = ExperienceLearner()
            lessons = learner.get_failure_lessons(limit=5)
            if lessons:
                for lesson in lessons:
                    self.bb.index_document(
                        f"pattern_{hash(str(lesson)) % 100000}",
                        str(lesson),
                    )
        except Exception:
            pass


    def _bg_improve_skill(self, goal: str):
        """Background: run SkillTrainer.improve_one() on the just-saved skill."""
        try:
            from skills import SkillTrainer, SkillLibrary
            from brain import _brain_state
            brain = getattr(_brain_state, 'brain_instance', None)
            if brain is None:
                return
            lib = SkillLibrary()
            matches = lib.find_skill(goal, min_similarity=0.85, limit=1)
            if not matches:
                return
            skill_name = matches[0].get("name", "")
            if not skill_name:
                return
            trainer = SkillTrainer(
                llm_fn=brain.quick_chat,
                thinking_fn=getattr(brain, 'thinking_chat', brain.quick_chat),
                skill_lib=lib,
            )
            report = trainer.improve_one(skill_name)
            if report and report.get("version_bumped"):
                logger.info(
                    f"[memory] Auto-improved '{skill_name}': "
                    f"{report['old_score']}→{report['new_score']}"
                )
        except Exception as e:
            logger.debug(f"[memory] bg improve failed: {e}")


def _brief(args: dict) -> str:
    if not args:
        return ""
    parts = [f"{k}={str(v)[:20]}" for k, v in list(args.items())[:2]]
    return ", ".join(parts)
