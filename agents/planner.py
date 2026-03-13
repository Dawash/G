"""
Planner Agent — Tree-of-Thoughts hierarchical planning.

Upgrades from the current one-shot DAG (cognitive.py ProblemSolver):
  - Explores multiple plan branches (branching factor 3)
  - Scores each branch via quick LLM evaluation
  - Picks best path (not just first)
  - Produces dependency-aware PlanNode graph
  - Can replan from any checkpoint on failure

Algorithm: Tree-of-Thoughts (ToT) with beam search.
  Level 0: Goal
  Level 1: 3 possible high-level approaches
  Level 2: Best approach decomposed into concrete steps
  Level 3: Each step gets tool hints + dependency edges
"""

import json
import logging
import re
from .base import BaseAgent
from .blackboard import PlanNode

logger = logging.getLogger(__name__)

# Max steps in a plan (prevent runaway)
MAX_PLAN_STEPS = 15
# How many alternative approaches to consider
BRANCH_FACTOR = 3
# Min score to accept a plan branch (0-100)
MIN_BRANCH_SCORE = 40


class PlannerAgent(BaseAgent):
    """Tree-of-Thoughts planner that generates hierarchical execution plans."""

    name = "planner"
    role = "Strategic planner that decomposes complex goals into executable steps"

    def run(self, goal: str = "", available_tools: list = None, **kwargs) -> dict:
        """Generate an execution plan for the goal.

        Args:
            goal: The user's goal to accomplish.
            available_tools: List of tool names the executor can use.

        Returns:
            {"status": "ok", "plan": [PlanNode, ...], "approach": str}
        """
        goal = goal or self.bb.get("goal", "")
        if not goal:
            return {"status": "error", "result": "No goal provided"}

        self._log(f"Planning: {goal[:80]}")
        self.bb.set("phase", "planning")

        # --- Step 1: Check skill library for existing plan ---
        cached_plan = self._check_skill_library(goal)
        if cached_plan:
            self._log(f"Found cached skill plan ({len(cached_plan)} steps)")
            self.bb.set_plan(cached_plan)
            return {"status": "ok", "plan": cached_plan, "approach": "skill_replay"}

        # --- Step 2: Classify complexity ---
        complexity = self._classify(goal)
        self._log(f"Complexity: {complexity}")

        if complexity == "simple":
            # Single-step plan
            plan = [PlanNode(id="1", description=goal, tool_hint=self._guess_tool(goal))]
            self.bb.set_plan(plan)
            return {"status": "ok", "plan": plan, "approach": "direct"}

        # --- Step 3: Tree-of-Thoughts — generate candidate approaches ---
        approaches = self._generate_approaches(goal, available_tools)
        if not approaches:
            # Fallback: single linear plan
            plan = self._linear_plan(goal, available_tools)
            self.bb.set_plan(plan)
            return {"status": "ok", "plan": plan, "approach": "linear_fallback"}

        # --- Step 4: Score approaches ---
        best = self._score_and_select(goal, approaches)
        self._log(f"Selected approach: {best['approach'][:80]} (score={best['score']})")

        # --- Step 5: Decompose best approach into concrete steps ---
        plan = self._decompose(goal, best["approach"], available_tools)

        # --- Step 6: Add dependency edges ---
        plan = self._add_dependencies(plan)

        self.bb.set_plan(plan)
        self._post({"approach": best["approach"], "steps": len(plan), "score": best["score"]},
                    msg_type="result")

        return {"status": "ok", "plan": plan, "approach": best["approach"]}

    def replan(self, goal: str, failed_step: str, error: str, **kwargs) -> dict:
        """Replan from failure point, avoiding the failed approach.

        Called by orchestrator when critic reports repeated failures.
        """
        self._log(f"Replanning from failure: {failed_step[:60]} -> {error[:60]}")

        # Get what's already done
        progress = self.bb.get_plan_progress()
        done_steps = [n for n in self.bb.get("plan", []) if n.status == "done"]
        done_summary = "; ".join(n.description for n in done_steps[-5:]) if done_steps else "none"

        prompt = (
            f"A task failed partway through. Replan the remaining work.\n\n"
            f"Original goal: {goal}\n"
            f"Steps completed: {done_summary}\n"
            f"Failed step: {failed_step}\n"
            f"Error: {error}\n"
            f"Progress: {progress['done']}/{progress['total']} done\n\n"
            f"Generate a NEW plan for the REMAINING work only. "
            f"Avoid the approach that failed. Try a different strategy.\n"
            f"Return a JSON array of steps: "
            f'[{{"id": "1", "step": "description", "tool": "tool_name"}}, ...]\n'
            f"Max {MAX_PLAN_STEPS} steps. JSON only, no explanation."
        )

        raw = self._llm_call(prompt)
        plan = self._parse_plan_json(raw, goal)

        if plan:
            self.bb.set_plan(plan)
            return {"status": "ok", "plan": plan, "approach": "replan_after_failure"}

        return {"status": "error", "result": "Replan failed"}

    # --- Internal Methods ---

    def _check_skill_library(self, goal: str) -> list | None:
        """Check if skill library has a matching plan."""
        try:
            from skills import SkillLibrary
            lib = SkillLibrary()
            matches = lib.find_skill(goal, min_similarity=0.75, limit=1)
            if matches and matches[0].get("similarity", 0) >= 0.75:
                skill = matches[0]
                steps = skill.get("tool_sequence", [])
                if steps and isinstance(steps, list):
                    plan = []
                    for i, step in enumerate(steps):
                        tool = step.get("tool", "") if isinstance(step, dict) else ""
                        desc = step.get("description", str(step)) if isinstance(step, dict) else str(step)
                        plan.append(PlanNode(
                            id=str(i + 1),
                            description=desc,
                            tool_hint=tool,
                        ))
                    return plan
        except Exception as e:
            logger.debug(f"Skill library check failed: {e}")
        return None

    def _classify(self, goal: str) -> str:
        """Quick complexity classification without LLM."""
        lower = goal.lower()
        # Simple: single clear action
        simple_patterns = [
            r"^(?:open|close|launch|minimize|maximize)\s+\w+$",
            r"^(?:what|when|where|who)\b.{0,40}$",
            r"^(?:play|pause|stop|mute|unmute|next|previous)\b",
            r"^(?:set a reminder|turn on|turn off|toggle)\b",
        ]
        if any(re.search(p, lower) for p in simple_patterns):
            return "simple"

        # Complex: multi-step indicators
        complex_indicators = [
            r"\band\b.*\band\b",           # Two "and"s
            r"\bthen\b",                    # Sequential
            r"\bafter\b.*\b(do|open|go)\b", # Dependent
            r"\bcreate.*\b(and|with)\b.*\b(send|post|upload|share)\b",
            r"\bbook\b.*\bflight\b",
            r"\border\b.*\bpizza\b",
            r"\bplan\b.*\btrip\b",
            r"\bresearch\b.*\band\b.*\b(write|create|send)\b",
        ]
        if any(re.search(p, lower) for p in complex_indicators):
            return "complex"

        # Compound: two actions joined by "and" / "then"
        compound_patterns = [
            r"\b(open|launch|close|play|search|create|go)\b.+\band\b.+\b(open|go|play|search|type|click|navigate|close)\b",
            r"\band\s+then\b",
            r"\bafter\s+that\b",
        ]
        if any(re.search(p, lower) for p in compound_patterns):
            return "compound"

        # Medium: has action verbs and is long
        word_count = len(lower.split())
        if word_count > 10:
            return "compound"
        return "simple"

    def _guess_tool(self, step: str) -> str:
        """Guess the best tool for a single step."""
        lower = step.lower()
        tool_map = [
            (r"\b(open|launch)\b", "open_app"),
            (r"\b(close)\b", "close_app"),
            (r"\b(search|google)\b", "google_search"),
            (r"\b(weather|forecast)\b", "get_weather"),
            (r"\b(time|date|day)\b", "get_time"),
            (r"\b(remind|reminder)\b", "set_reminder"),
            (r"\b(screenshot|screen)\b", "take_screenshot"),
            (r"\b(play|music|song)\b", "play_music"),
            (r"\b(click)\b", "click_at"),
            (r"\b(type|enter text)\b", "type_text"),
            (r"\b(navigate|go to|visit|browse)\b", "browser_action"),
            (r"\b(create|make|build|generate)\b.*\b(file|page|script|app)\b", "create_file"),
            (r"\b(news|headline)\b", "get_news"),
            (r"\b(email|send mail)\b", "send_email"),
            (r"\b(terminal|command|powershell)\b", "run_terminal"),
        ]
        for pattern, tool in tool_map:
            if re.search(pattern, lower):
                return tool
        return ""

    def _generate_approaches(self, goal: str, tools: list = None) -> list:
        """Generate N different high-level approaches (ToT branching)."""
        tools_str = ", ".join(tools[:20]) if tools else "open_app, close_app, google_search, browser_action, click_at, type_text, take_screenshot, run_terminal, create_file, play_music"

        prompt = (
            f"You are a strategic planner for a Windows desktop AI assistant.\n\n"
            f"Goal: \"{goal}\"\n"
            f"Available tools: {tools_str}\n\n"
            f"Generate exactly {BRANCH_FACTOR} DIFFERENT high-level approaches "
            f"to accomplish this goal. Each approach should use a different strategy.\n\n"
            f"Return JSON array:\n"
            f'[{{"approach": "description of approach 1"}}, '
            f'{{"approach": "description of approach 2"}}, '
            f'{{"approach": "description of approach 3"}}]\n\n'
            f"JSON only, no explanation."
        )

        raw = self._llm_call(prompt)
        return self._parse_approaches(raw)

    def _parse_approaches(self, raw: str) -> list:
        """Parse LLM output into list of approach dicts, with robust fallback."""
        # Strip markdown code blocks
        cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip()

        # Try JSON parse
        try:
            match = re.search(r'\[.*\]', cleaned, re.DOTALL)
            if match:
                approaches = json.loads(match.group())
                result = [a for a in approaches if isinstance(a, dict) and "approach" in a]
                if result:
                    return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Try line-by-line JSON objects
        approaches = []
        for line in cleaned.split("\n"):
            line = line.strip().rstrip(",")
            if line.startswith("{") and "approach" in line:
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "approach" in obj:
                        approaches.append(obj)
                except (json.JSONDecodeError, ValueError):
                    pass

        if approaches:
            return approaches[:BRANCH_FACTOR]

        # Last fallback: numbered list extraction
        approaches = []
        for line in raw.split("\n"):
            line = line.strip().lstrip("0123456789.-) *")
            # Skip lines that look like JSON keys
            if line.startswith("{") or line.startswith("["):
                continue
            if line and len(line) > 10:
                approaches.append({"approach": line})
        return approaches[:BRANCH_FACTOR]

    def _score_and_select(self, goal: str, approaches: list) -> dict:
        """Score each approach and pick the best one."""
        if len(approaches) == 1:
            return {**approaches[0], "score": 70}

        # Check past reflexions for hints
        reflexions = self.bb.get("reflexions", [])
        reflexion_hint = ""
        if reflexions:
            reflexion_hint = f"\nLessons from past failures: {'; '.join(reflexions[-3:])}\n"

        descriptions = "\n".join(
            f"  {i+1}. {a['approach']}" for i, a in enumerate(approaches)
        )
        prompt = (
            f"Score these approaches for the goal: \"{goal}\"\n\n"
            f"Approaches:\n{descriptions}\n"
            f"{reflexion_hint}\n"
            f"For each approach, rate feasibility 0-100 considering:\n"
            f"- Can it be done with desktop automation tools?\n"
            f"- How many steps are needed?\n"
            f"- How likely is it to succeed?\n\n"
            f'Return JSON: [{{"id": 1, "score": 85}}, {{"id": 2, "score": 60}}, ...]\n'
            f"JSON only."
        )

        raw = self._llm_call(prompt)
        scores = {}
        try:
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                scored = json.loads(match.group())
                for s in scored:
                    idx = s.get("id", 0) - 1
                    if 0 <= idx < len(approaches):
                        scores[idx] = s.get("score", 50)
        except (json.JSONDecodeError, ValueError):
            pass

        # Assign scores
        for i, a in enumerate(approaches):
            a["score"] = scores.get(i, 50)

        # Pick best
        best = max(approaches, key=lambda a: a.get("score", 0))
        return best

    def _decompose(self, goal: str, approach: str, tools: list = None) -> list:
        """Decompose chosen approach into concrete executable steps."""
        tools_str = ", ".join(tools[:20]) if tools else "open_app, close_app, google_search, browser_action, click_at, type_text, press_key, take_screenshot, run_terminal, create_file"

        prompt = (
            f"Decompose this plan into concrete executable steps.\n\n"
            f"Goal: \"{goal}\"\n"
            f"Approach: {approach}\n"
            f"Available tools: {tools_str}\n\n"
            f"Rules:\n"
            f"- Each step must be a SINGLE action (one tool call)\n"
            f"- Include the tool name for each step\n"
            f"- Mark steps requiring user input as takeover=true (login, payment, CAPTCHA)\n"
            f"- Max {MAX_PLAN_STEPS} steps\n\n"
            f"Return JSON array:\n"
            f'[{{"id": "1", "step": "Open Chrome browser", "tool": "open_app", "takeover": false}}, ...]\n'
            f"JSON only."
        )

        raw = self._llm_call(prompt)
        return self._parse_plan_json(raw, goal)

    def _linear_plan(self, goal: str, tools: list = None) -> list:
        """Fallback: generate a simple linear plan without ToT."""
        tools_str = ", ".join(tools[:20]) if tools else "open_app, close_app, google_search, browser_action, click_at, type_text, take_screenshot, run_terminal, create_file"

        prompt = (
            f"Break this task into ordered steps for a desktop automation agent.\n\n"
            f"Task: \"{goal}\"\n"
            f"Available tools: {tools_str}\n\n"
            f"Return JSON array of steps:\n"
            f'[{{"id": "1", "step": "description", "tool": "tool_name"}}, ...]\n'
            f"Max {MAX_PLAN_STEPS} steps. JSON only."
        )

        raw = self._llm_call(prompt)
        return self._parse_plan_json(raw, goal)

    def _parse_plan_json(self, raw: str, goal: str) -> list:
        """Parse LLM output into PlanNode list."""
        # Strip markdown code blocks
        cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip()
        plan = []
        try:
            match = re.search(r'\[.*\]', cleaned, re.DOTALL)
            if match:
                steps = json.loads(match.group())
                for s in steps[:MAX_PLAN_STEPS]:
                    if isinstance(s, dict):
                        plan.append(PlanNode(
                            id=str(s.get("id", len(plan) + 1)),
                            description=s.get("step", s.get("description", "")),
                            tool_hint=s.get("tool", ""),
                            takeover=s.get("takeover", False),
                        ))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Plan JSON parse failed: {e}")

        if not plan:
            # Last resort: treat entire goal as one step
            plan = [PlanNode(id="1", description=goal)]

        return plan

    def _add_dependencies(self, plan: list) -> list:
        """Add sequential dependencies: each step depends on the previous."""
        for i in range(1, len(plan)):
            plan[i].deps = [plan[i - 1].id]
        return plan
