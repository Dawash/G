"""
Skill Engine — JARVIS/HuggingGPT pattern implementation.

The core idea:
  1. User makes a request
  2. Planner LLM decomposes it into sub-tasks
  3. Each sub-task is matched to a skill
  4. Skills execute in dependency order (DAG)
  5. Response Generator LLM summarizes all results

Skills are self-contained modules with:
  - Typed input/output schemas
  - Can call other skills (composition)
  - Track success/failure for reliability scoring
  - Auto-improve via learned skills from past interactions

Architecture:
  ┌─────────────┐     ┌──────────┐     ┌──────────┐
  │   Planner   │ ──> │ Executor │ ──> │ Reporter │
  │  (LLM plan) │     │ (skills) │     │ (LLM sum)│
  └─────────────┘     └──────────┘     └──────────┘
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Skill Definition
# ===================================================================

@dataclass
class Skill:
    """A self-contained capability the agent can use.

    Skills are like microservices — each does ONE thing well,
    with a clear input/output contract.
    """
    name: str
    description: str                     # What it does (shown to LLM planner)
    category: str = "general"            # For grouping: system, media, web, productivity
    input_schema: dict = field(default_factory=dict)   # JSON Schema for inputs
    output_type: str = "text"            # text, json, boolean, number
    handler: Callable = None             # Function(inputs: dict) -> result
    depends_on: list = field(default_factory=list)     # Skills that must run first
    examples: list = field(default_factory=list)       # Example inputs for matching
    success_count: int = 0
    fail_count: int = 0
    avg_time_ms: float = 0

    @property
    def reliability(self):
        """Success rate 0.0-1.0."""
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.5

    def execute(self, inputs: dict) -> Any:
        """Run the skill with given inputs."""
        if not self.handler:
            raise ValueError(f"Skill '{self.name}' has no handler")
        start = time.time()
        try:
            result = self.handler(inputs)
            elapsed = (time.time() - start) * 1000
            self.success_count += 1
            self.avg_time_ms = (self.avg_time_ms * (self.success_count - 1) + elapsed) / self.success_count
            return result
        except Exception as e:
            self.fail_count += 1
            logger.error(f"Skill '{self.name}' failed: {e}")
            raise


@dataclass
class TaskStep:
    """A single step in a task plan."""
    skill_name: str
    inputs: dict = field(default_factory=dict)
    depends_on: list = field(default_factory=list)  # Step indices this depends on
    result: Any = None
    status: str = "pending"  # pending, running, done, failed


@dataclass
class TaskPlan:
    """A plan decomposed from user request."""
    goal: str
    steps: list = field(default_factory=list)  # List of TaskStep
    created_at: float = field(default_factory=time.time)


# ===================================================================
# Skill Registry
# ===================================================================

class SkillRegistry:
    """Central registry of all available skills."""

    def __init__(self):
        self._skills = {}  # name -> Skill
        self._categories = {}  # category -> [skill_names]

    def register(self, skill: Skill):
        """Register a skill."""
        self._skills[skill.name] = skill
        cat = skill.category
        if cat not in self._categories:
            self._categories[cat] = []
        if skill.name not in self._categories[cat]:
            self._categories[cat].append(skill.name)
        logger.debug(f"Skill registered: {skill.name} [{cat}]")

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def find_by_category(self, category: str) -> list:
        """Get all skills in a category."""
        names = self._categories.get(category, [])
        return [self._skills[n] for n in names if n in self._skills]

    def search(self, query: str, limit: int = 5) -> list:
        """Find skills matching a query by description keyword match."""
        query_words = set(query.lower().split())
        scored = []
        for skill in self._skills.values():
            desc_words = set(skill.description.lower().split())
            overlap = len(query_words & desc_words)
            if overlap > 0:
                scored.append((overlap, skill.reliability, skill))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [s[2] for s in scored[:limit]]

    def get_catalog(self) -> str:
        """Build a skill catalog string for the LLM planner."""
        lines = []
        for cat, names in sorted(self._categories.items()):
            lines.append(f"\n[{cat.upper()}]")
            for name in names:
                skill = self._skills[name]
                inputs = ", ".join(f"{k}: {v.get('type', 'string')}"
                                   for k, v in skill.input_schema.get("properties", {}).items())
                reliability = f" ({skill.reliability:.0%} reliable)" if skill.success_count > 0 else ""
                lines.append(f"  - {name}({inputs}): {skill.description}{reliability}")
        return "\n".join(lines)

    @property
    def count(self):
        return len(self._skills)

    @property
    def names(self):
        return list(self._skills.keys())


# ===================================================================
# Task Planner (JARVIS Step 1: LLM decomposes request into skill chain)
# ===================================================================

class TaskPlanner:
    """Uses LLM to decompose a complex request into a skill chain."""

    def __init__(self, quick_chat_fn, registry: SkillRegistry):
        self._chat = quick_chat_fn
        self._registry = registry

    def plan(self, user_request: str) -> Optional[TaskPlan]:
        """Decompose a user request into a TaskPlan.

        The LLM sees the skill catalog and creates a step-by-step plan.
        Each step maps to a skill with specific inputs.
        """
        catalog = self._registry.get_catalog()
        if not catalog.strip():
            return None

        prompt = (
            f"You are a task planner. Break down the user's request into steps.\n"
            f"Each step uses ONE skill from the catalog below.\n\n"
            f"SKILL CATALOG:{catalog}\n\n"
            f"USER REQUEST: \"{user_request}\"\n\n"
            f"OUTPUT FORMAT (JSON only, no other text):\n"
            f'{{"steps": [\n'
            f'  {{"skill": "skill_name", "inputs": {{"key": "value"}}, "depends_on": []}},\n'
            f'  {{"skill": "skill_name", "inputs": {{"key": "value"}}, "depends_on": [0]}}\n'
            f']}}\n\n'
            f"RULES:\n"
            f"- Use ONLY skills from the catalog\n"
            f"- depends_on lists step INDICES (0-based) that must complete first\n"
            f"- For simple requests, use just 1 step\n"
            f"- Inputs should be specific values, not placeholders\n"
            f"- Output ONLY valid JSON, nothing else"
        )

        try:
            response = self._chat(prompt)
            if not response:
                return None

            # Extract JSON from response
            import re
            json_match = re.search(r'\{[\s\S]*\}', response)
            if not json_match:
                return None

            data = json.loads(json_match.group())
            steps = []
            for s in data.get("steps", []):
                skill_name = s.get("skill", "")
                if skill_name not in self._registry.names:
                    logger.warning(f"Planner suggested unknown skill: {skill_name}")
                    continue
                steps.append(TaskStep(
                    skill_name=skill_name,
                    inputs=s.get("inputs", {}),
                    depends_on=s.get("depends_on", []),
                ))

            if not steps:
                return None

            plan = TaskPlan(goal=user_request, steps=steps)
            logger.info(f"Plan created: {len(steps)} steps for '{user_request[:50]}'")
            return plan

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Plan parsing failed: {e}")
            return None


# ===================================================================
# Task Executor (JARVIS Step 2: Execute skills in dependency order)
# ===================================================================

class TaskExecutor:
    """Executes a TaskPlan by running skills in dependency order."""

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def execute(self, plan: TaskPlan, timeout: float = 60.0) -> list:
        """Execute all steps in dependency order.

        Returns list of (step_index, skill_name, result, status).
        """
        start = time.time()
        results = []

        for i, step in enumerate(plan.steps):
            if time.time() - start > timeout:
                step.status = "timeout"
                results.append((i, step.skill_name, "Timed out", "timeout"))
                continue

            # Check dependencies
            deps_ok = all(
                plan.steps[d].status == "done"
                for d in step.depends_on
                if d < len(plan.steps)
            )
            if not deps_ok:
                step.status = "skipped"
                results.append((i, step.skill_name, "Skipped (dependency failed)", "skipped"))
                continue

            # Inject results from dependencies into inputs
            for dep_idx in step.depends_on:
                if dep_idx < len(plan.steps) and plan.steps[dep_idx].result:
                    step.inputs[f"_step_{dep_idx}_result"] = str(plan.steps[dep_idx].result)

            # Execute
            skill = self._registry.get(step.skill_name)
            if not skill:
                step.status = "failed"
                results.append((i, step.skill_name, f"Skill not found: {step.skill_name}", "failed"))
                continue

            step.status = "running"
            try:
                result = skill.execute(step.inputs)
                step.result = result
                step.status = "done"
                results.append((i, step.skill_name, result, "done"))
                logger.info(f"Step {i} ({step.skill_name}): done")
            except Exception as e:
                step.status = "failed"
                step.result = str(e)
                results.append((i, step.skill_name, str(e), "failed"))
                logger.warning(f"Step {i} ({step.skill_name}): failed — {e}")

        return results


# ===================================================================
# Response Generator (JARVIS Step 3: LLM summarizes results)
# ===================================================================

class ResponseGenerator:
    """Summarizes execution results into a natural spoken response."""

    def __init__(self, quick_chat_fn):
        self._chat = quick_chat_fn

    def summarize(self, goal: str, results: list) -> str:
        """Create a natural response from execution results.

        For simple single-step results, just returns the result directly.
        For multi-step, asks LLM to summarize.
        """
        # Single step with clean result — return directly
        if len(results) == 1:
            _, skill, result, status = results[0]
            if status == "done" and result:
                return str(result)
            elif status == "failed":
                return f"I tried but couldn't complete that: {result}"

        # Multi-step — summarize with LLM
        step_summary = []
        for i, skill, result, status in results:
            step_summary.append(f"Step {i+1} ({skill}): {status} — {str(result)[:100]}")

        all_done = all(s == "done" for _, _, _, s in results)
        if all_done and self._chat:
            try:
                summary = self._chat(
                    f"The user asked: \"{goal}\"\n"
                    f"Here's what was done:\n" + "\n".join(step_summary) + "\n\n"
                    f"Give a brief natural spoken summary (1-2 sentences). "
                    f"Focus on what was accomplished, not the steps."
                )
                if summary and len(summary) > 5:
                    return summary
            except Exception:
                pass

        # Fallback: join results
        done_results = [str(r) for _, _, r, s in results if s == "done" and r]
        if done_results:
            return " ".join(done_results[:3])
        return "I had trouble completing that task."


# ===================================================================
# JARVIS Engine (unified orchestrator)
# ===================================================================

class JarvisEngine:
    """JARVIS/HuggingGPT-style skill execution engine.

    Usage:
        engine = JarvisEngine(quick_chat_fn=brain.quick_chat)
        engine.register_builtin_skills(action_registry)

        # Simple request (1 skill):
        result = engine.run("what's the weather")
        # → plans: [get_weather({})] → executes → "It's 45°F in Düsseldorf"

        # Complex request (skill chain):
        result = engine.run("check the weather and if it's cold remind me to bring a jacket")
        # → plans: [get_weather({}), set_reminder({message: "bring jacket", ...})]
        # → executes in order → summarizes
    """

    def __init__(self, quick_chat_fn=None):
        self.registry = SkillRegistry()
        self._chat = quick_chat_fn
        self._planner = TaskPlanner(quick_chat_fn, self.registry) if quick_chat_fn else None
        self._executor = TaskExecutor(self.registry)
        self._reporter = ResponseGenerator(quick_chat_fn) if quick_chat_fn else None

    def register(self, skill: Skill):
        """Register a skill."""
        self.registry.register(skill)

    def register_builtin_skills(self, action_registry=None, reminder_mgr=None):
        """Register built-in skills from the existing tool system."""
        _reg = action_registry or {}

        # --- System skills ---
        self.register(Skill(
            name="get_weather",
            description="Get current weather for a city or auto-detected location",
            category="system",
            input_schema={"type": "object", "properties": {
                "city": {"type": "string", "description": "City name (optional, auto-detects if empty)"}
            }},
            handler=lambda inputs: _import_call("weather", "get_current_weather", inputs.get("city")),
            examples=["what's the weather", "weather in tokyo", "is it cold outside"],
        ))

        self.register(Skill(
            name="get_forecast",
            description="Get hourly weather forecast for the next 6 hours",
            category="system",
            input_schema={"type": "object", "properties": {
                "city": {"type": "string", "description": "City name (optional)"}
            }},
            handler=lambda inputs: _import_call("weather", "get_forecast", inputs.get("city")),
            examples=["forecast", "will it rain today", "forecast for london"],
        ))

        self.register(Skill(
            name="get_time",
            description="Get current time and date",
            category="system",
            input_schema={"type": "object", "properties": {}},
            handler=lambda inputs: _get_time(),
            examples=["what time is it", "what day is it"],
        ))

        self.register(Skill(
            name="get_news",
            description="Get latest news headlines by category",
            category="system",
            input_schema={"type": "object", "properties": {
                "category": {"type": "string", "description": "News category: general, tech, sports, science, business"}
            }},
            handler=lambda inputs: _import_call("news", "get_news", inputs.get("category", "general")),
            examples=["get me the news", "tech news", "sports headlines"],
        ))

        # --- App skills ---
        if "open_app" in _reg:
            self.register(Skill(
                name="open_app",
                description="Open/launch an application by name",
                category="apps",
                input_schema={"type": "object", "properties": {
                    "name": {"type": "string", "description": "App name to open"}
                }, "required": ["name"]},
                handler=lambda inputs: _reg["open_app"](inputs["name"]),
                examples=["open chrome", "launch spotify", "start notepad"],
            ))

        if "close_app" in _reg:
            self.register(Skill(
                name="close_app",
                description="Close a running application",
                category="apps",
                input_schema={"type": "object", "properties": {
                    "name": {"type": "string", "description": "App name to close"}
                }, "required": ["name"]},
                handler=lambda inputs: _reg["close_app"](inputs["name"]),
                examples=["close chrome", "quit notepad"],
            ))

        # --- Media skills ---
        self.register(Skill(
            name="play_music",
            description="Play music, control playback (play/pause/next/previous/volume)",
            category="media",
            input_schema={"type": "object", "properties": {
                "action": {"type": "string", "description": "play, pause, next, previous, volume_up, volume_down"},
                "query": {"type": "string", "description": "Song/artist/genre to play"},
                "app": {"type": "string", "description": "spotify or youtube"}
            }},
            handler=lambda inputs: _play_music_skill(inputs),
            examples=["play jazz", "pause music", "next song", "play something on spotify"],
        ))

        # --- Productivity skills ---
        if reminder_mgr:
            self.register(Skill(
                name="set_reminder",
                description="Set a reminder for a specific time with a message",
                category="productivity",
                input_schema={"type": "object", "properties": {
                    "message": {"type": "string", "description": "What to remind about"},
                    "time": {"type": "string", "description": "When to remind (e.g., '5pm', 'in 30 minutes')"}
                }, "required": ["message", "time"]},
                handler=lambda inputs: _set_reminder_skill(inputs, reminder_mgr),
                examples=["remind me to call mom at 5pm", "set a reminder for in 30 minutes to stretch"],
            ))

        # --- Web skills ---
        self.register(Skill(
            name="web_search",
            description="Search the web for information",
            category="web",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"}
            }, "required": ["query"]},
            handler=lambda inputs: _web_search_skill(inputs),
            examples=["search for python tutorials", "look up flight prices to paris"],
        ))

        self.register(Skill(
            name="system_info",
            description="Get system information: battery, disk, CPU, RAM, IP, processes",
            category="system",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "What to check: battery, disk, cpu, ram, ip, processes"}
            }, "required": ["query"]},
            handler=lambda inputs: _system_info_skill(inputs),
            examples=["check my battery", "how much disk space", "cpu usage"],
        ))

        logger.info(f"JARVIS engine: {self.registry.count} skills registered")

    def run(self, user_request: str, timeout: float = 60.0) -> Optional[str]:
        """Run the full JARVIS pipeline: Plan → Execute → Summarize.

        For simple requests (1 skill), skips LLM planning overhead.
        For complex requests, uses LLM to decompose into skill chain.
        """
        if not user_request or not user_request.strip():
            return None

        # Quick match: see if any skill's examples match directly
        direct = self._try_direct_match(user_request)
        if direct:
            skill, inputs = direct
            try:
                result = skill.execute(inputs)
                return str(result) if result else None
            except Exception as e:
                return f"Couldn't complete that: {e}"

        # Complex request: use LLM planner
        if not self._planner:
            return None

        plan = self._planner.plan(user_request)
        if not plan or not plan.steps:
            return None

        # Execute the plan
        results = self._executor.execute(plan, timeout=timeout)

        # Summarize
        if self._reporter:
            return self._reporter.summarize(user_request, results)

        # Fallback: return last result
        done = [r for _, _, r, s in results if s == "done" and r]
        return done[-1] if done else "Task completed."

    def _try_direct_match(self, text: str) -> Optional[tuple]:
        """Quick keyword match against skill examples. No LLM needed."""
        lower = text.lower().strip()
        for skill in self.registry._skills.values():
            for example in skill.examples:
                if example.lower() in lower or lower in example.lower():
                    # Extract basic inputs from the text
                    inputs = self._extract_inputs(lower, skill)
                    return skill, inputs
        return None

    def _extract_inputs(self, text: str, skill: Skill) -> dict:
        """Simple input extraction from text based on skill schema."""
        inputs = {}
        props = skill.input_schema.get("properties", {})

        # For skills with a single string input, use the text
        if len(props) == 1:
            key = list(props.keys())[0]
            inputs[key] = text
        elif "city" in props:
            import re
            m = re.search(r'(?:in|for|at)\s+(.+?)(?:\?|$)', text)
            if m:
                inputs["city"] = m.group(1).strip()
        elif "query" in props:
            inputs["query"] = text
        elif "name" in props:
            import re
            m = re.search(r'(?:open|close|launch|start|quit|exit)\s+(.+)', text)
            if m:
                inputs["name"] = m.group(1).strip()

        return inputs

    def get_stats(self) -> dict:
        """Get skill usage statistics."""
        stats = {}
        for name, skill in self.registry._skills.items():
            if skill.success_count + skill.fail_count > 0:
                stats[name] = {
                    "success": skill.success_count,
                    "fail": skill.fail_count,
                    "reliability": f"{skill.reliability:.0%}",
                    "avg_ms": f"{skill.avg_time_ms:.0f}",
                }
        return stats


# ===================================================================
# Skill handler helpers
# ===================================================================

def _import_call(module_name, func_name, *args, **kwargs):
    """Dynamically import and call a function."""
    import importlib
    mod = importlib.import_module(module_name)
    fn = getattr(mod, func_name)
    # Filter None args
    clean_args = [a for a in args if a is not None]
    return fn(*clean_args, **kwargs) if clean_args else fn(**kwargs)


def _get_time():
    from datetime import datetime
    now = datetime.now()
    return f"It's {now.strftime('%A')}, {now.strftime('%I:%M %p').lstrip('0')}."


def _play_music_skill(inputs):
    from platform_impl.windows.media import play_music
    action = inputs.get("action", "play")
    query = inputs.get("query", "")
    app = inputs.get("app", "spotify")
    return play_music(action, query, app)


def _set_reminder_skill(inputs, mgr):
    time_str = inputs.get("time", "in 1 hour")
    message = inputs.get("message", "reminder")
    result = mgr.add(message, time_str)
    return result


def _web_search_skill(inputs):
    try:
        from web_agent import web_search_extract
        return web_search_extract(inputs.get("query", ""))
    except ImportError:
        return "Web search is not available."


def _system_info_skill(inputs):
    import subprocess
    query = inputs.get("query", "").lower()
    cmd = None
    if "battery" in query:
        cmd = "WMIC PATH Win32_Battery Get EstimatedChargeRemaining,Status /VALUE"
    elif "disk" in query:
        cmd = "Get-PSDrive C,D -ErrorAction SilentlyContinue | Select-Object Name,@{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}},@{N='Used(GB)';E={[math]::Round($_.Used/1GB,1)}} | Format-Table -AutoSize"
    elif "cpu" in query:
        cmd = "(Get-CimInstance Win32_Processor).LoadPercentage"
    elif "ram" in query or "memory" in query:
        cmd = "Get-Process | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First 5 Name,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize"
    elif "ip" in query:
        cmd = "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -ne '127.0.0.1'} | Select-Object InterfaceAlias,IPAddress | Format-Table -AutoSize"
    elif "process" in query:
        cmd = "(Get-Process).Count"
    if cmd:
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or "No output."
        except Exception as e:
            return f"Error: {e}"
    return "Specify what to check: battery, disk, cpu, ram, ip, or processes."
