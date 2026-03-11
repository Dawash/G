"""
Cognitive Engine — simulates human learning, comprehension, problem solving,
decision making, creativity, and autonomy.

Architecture:
  CognitiveEngine (coordinator)
    ├── ExperienceLearner    — Phase 1: learns from every tool outcome
    ├── ContextComprehender  — Phase 2: deep multi-turn understanding
    ├── ProblemSolver        — Phase 3: decomposes complex goals
    ├── DecisionEngine       — Phase 4: confidence-weighted choices
    ├── CreativeEngine       — Phase 5: novel solutions and suggestions
    └── AutonomyEngine       — Phase 6: self-directed improvement

All phases share one SQLite DB (memory.db) and one thread lock.
Each phase can function independently — no cross-phase runtime deps.
Integration: brain.py creates CognitiveEngine and calls its hooks.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")

# Shared lock for all cognitive DB writes
_db_lock = threading.Lock()


# =====================================================================
# Utility: lightweight LLM call (avoids circular import with brain.py)
# =====================================================================

_cached_config = None

def _get_provider_config():
    """Load and cache provider config for cognitive LLM calls."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    try:
        from config import load_config, DEFAULT_OLLAMA_URL, DEFAULT_OLLAMA_MODEL
        cfg = load_config()
        _cached_config = {
            "provider": cfg.get("provider", "ollama"),
            "api_key": cfg.get("api_key", ""),
            "model": cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL),
            "ollama_url": cfg.get("ollama_url", DEFAULT_OLLAMA_URL),
        }
    except Exception:
        _cached_config = {"provider": "ollama", "api_key": "", "model": "qwen2.5:7b",
                          "ollama_url": "http://localhost:11434"}
    return _cached_config


def _llm_call(prompt, max_tokens=200, temperature=0.3):
    """Single-turn LLM call using the configured provider. Returns text or None."""
    try:
        import requests
        cfg = _get_provider_config()
        provider = cfg["provider"]

        ollama_url = cfg.get("ollama_url", "http://localhost:11434").rstrip("/")

        if provider == "ollama":
            resp = requests.post(
                f"{ollama_url}/v1/chat/completions",
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

        elif provider == "openai":
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

        elif provider == "anthropic":
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": cfg["api_key"],
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()

        elif provider == "openrouter":
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

        else:
            # Unknown provider, try Ollama as fallback
            resp = requests.post(
                f"{ollama_url}/v1/chat/completions",
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

    except Exception as e:
        logger.debug(f"Cognitive LLM call failed: {e}")
        return None


# =====================================================================
# Shared DB connection factory
# =====================================================================

def _get_db(db_path=DB_FILE):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# =====================================================================
# REQUEST PATTERN EXTRACTION — shared by multiple phases
# =====================================================================

_PATTERN_MAP = [
    (r"play\s+.+\s+(on|in)\s+(spotify|youtube)", "play_music_on_app"),
    (r"play\s+(some|a|the)?\s*(good|romantic|sad|chill|happy)?\s*music", "play_music_genre"),
    (r"play\s+.+", "play_music_query"),
    (r"(open|launch|start)\s+.+", "open_app"),
    (r"(close|kill|quit)\s+.+", "close_app"),
    (r"(what'?s? the |check |get )(weather|temperature)", "weather"),
    (r"weather\s+in\s+.+", "weather_city"),
    (r"(forecast|will it rain)", "forecast"),
    (r"(set|create|add)\s+(a )?reminder", "set_reminder"),
    (r"(what'?s? the |check |get )(time|date)", "get_time"),
    (r"(search|look up|google)\s+.+", "web_search"),
    (r"(install|uninstall|update)\s+.+", "manage_software"),
    (r"(move|copy|rename|delete|zip|find)\s+.+", "manage_files"),
    (r"(disk space|how much ram|cpu|memory|my ip|ping)", "system_info"),
    (r"(create|make|build|write)\s+.*(file|page|script|html|calculator)", "create_file"),
    (r"(news|headlines)", "news"),
    (r"(send|compose|write)\s+.*(email|mail)", "send_email"),
    (r"(turn on|turn off|toggle|enable|disable)", "toggle_setting"),
]


def extract_pattern(text):
    """Map user request to a reusable pattern key."""
    if not text:
        return "unknown"
    lower = text.lower().strip()
    for regex, pattern in _PATTERN_MAP:
        if re.search(regex, lower):
            return pattern
    return "general_chat"


# =====================================================================
# PHASE 1: EXPERIENCE LEARNER — learns from every tool outcome
# =====================================================================

class ExperienceLearner:
    """Tracks tool outcomes and builds proven strategies over time.

    Writes: experience_log, strategies, failure_lessons tables.
    Reads by: DecisionEngine (confidence), AutonomyEngine (self-analysis).
    """

    def __init__(self, conn):
        self._conn = conn
        self._init_tables()

    def _init_tables(self):
        c = self._conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS experience_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                request_pattern TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT,
                outcome TEXT NOT NULL,
                result_summary TEXT,
                recovery_tool TEXT,
                recovery_args TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                best_tool TEXT NOT NULL,
                best_args_template TEXT,
                success_count INTEGER DEFAULT 1,
                fail_count INTEGER DEFAULT 0,
                advice TEXT,
                updated_at REAL NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS failure_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                error_pattern TEXT NOT NULL,
                fix_description TEXT NOT NULL,
                fix_tool TEXT,
                fix_args TEXT,
                occurrences INTEGER DEFAULT 1,
                last_seen REAL NOT NULL,
                UNIQUE(tool_name, error_pattern)
            )
        """)
        self._conn.commit()

    # --- Write ---

    def log_outcome(self, request_text, tool_name, arguments, success, result_text=""):
        pattern = extract_pattern(request_text)
        outcome = "success" if success else "failure"
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT INTO experience_log
                (timestamp, request_pattern, tool_name, arguments, outcome, result_summary)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), pattern, tool_name,
                  json.dumps(arguments) if arguments else None,
                  outcome, (result_text or "")[:200]))
            self._conn.commit()
        if success:
            self._reinforce(pattern, tool_name, arguments)
        else:
            self._weaken(pattern, tool_name)

    def log_recovery(self, request_text, original_tool, recovery_tool, recovery_args):
        pattern = extract_pattern(request_text)
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                UPDATE experience_log SET recovery_tool = ?, recovery_args = ?
                WHERE id = (
                    SELECT id FROM experience_log
                    WHERE request_pattern = ? AND tool_name = ? AND outcome = 'failure'
                    ORDER BY timestamp DESC LIMIT 1
                )
            """, (recovery_tool, json.dumps(recovery_args) if recovery_args else None,
                  pattern, original_tool))
            fix_desc = f"Use {recovery_tool} instead of {original_tool}"
            c.execute("""
                INSERT INTO failure_lessons
                (tool_name, error_pattern, fix_description, fix_tool, fix_args, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_name, error_pattern) DO UPDATE SET
                    fix_description = excluded.fix_description,
                    fix_tool = excluded.fix_tool,
                    fix_args = excluded.fix_args,
                    occurrences = occurrences + 1,
                    last_seen = excluded.last_seen
            """, (original_tool, f"{original_tool}_failed", fix_desc,
                  recovery_tool, json.dumps(recovery_args) if recovery_args else None,
                  time.time()))
            self._conn.commit()

    # --- Read ---

    def get_strategy(self, request_text):
        """Best tool for this request pattern, or None."""
        pattern = extract_pattern(request_text)
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT best_tool, advice, success_count, fail_count FROM strategies
                WHERE pattern = ? AND success_count > fail_count
                ORDER BY success_count DESC LIMIT 1
            """, (pattern,))
            row = c.fetchone()
        if row and row["success_count"] >= 2:
            return {"tool": row["best_tool"], "advice": row["advice"],
                    "confidence": row["success_count"] / max(row["success_count"] + row["fail_count"], 1)}
        return None

    def get_failure_lessons(self, limit=3):
        """Top failure patterns with known fixes."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT tool_name, fix_description, occurrences FROM failure_lessons
                WHERE occurrences >= 2
                ORDER BY occurrences DESC LIMIT ?
            """, (limit,))
            return [dict(row) for row in c.fetchall()]

    def get_failure_hint(self, tool_name):
        """Specific fix for a tool that failed."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT fix_description, fix_tool, fix_args FROM failure_lessons
                WHERE tool_name = ? AND occurrences >= 2
                ORDER BY occurrences DESC LIMIT 1
            """, (tool_name,))
            row = c.fetchone()
        if row:
            return {"description": row["fix_description"], "tool": row["fix_tool"],
                    "args": json.loads(row["fix_args"]) if row["fix_args"] else None}
        return None

    def get_success_rate(self, tool_name=None, days=7):
        cutoff = time.time() - (days * 86400)
        with _db_lock:
            c = self._conn.cursor()
            q = "SELECT outcome, COUNT(*) as cnt FROM experience_log WHERE timestamp > ?"
            params = [cutoff]
            if tool_name:
                q += " AND tool_name = ?"
                params.append(tool_name)
            q += " GROUP BY outcome"
            c.execute(q, params)
            counts = {row["outcome"]: row["cnt"] for row in c.fetchall()}
        total = sum(counts.values())
        if total == 0:
            return {"total": 0, "success": 0, "failure": 0, "rate": 1.0}
        return {"total": total, "success": counts.get("success", 0),
                "failure": counts.get("failure", 0),
                "rate": counts.get("success", 0) / total}

    def get_top_failures(self, limit=5, days=7):
        cutoff = time.time() - (days * 86400)
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT tool_name, request_pattern, COUNT(*) as cnt
                FROM experience_log WHERE outcome = 'failure' AND timestamp > ?
                GROUP BY tool_name, request_pattern ORDER BY cnt DESC LIMIT ?
            """, (cutoff, limit))
            return [dict(row) for row in c.fetchall()]

    # --- Internal ---

    def _reinforce(self, pattern, tool_name, arguments):
        tpl = json.dumps({k: type(v).__name__ for k, v in arguments.items()}) if isinstance(arguments, dict) else None
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT INTO strategies (pattern, best_tool, best_args_template, success_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(pattern) DO UPDATE SET
                    best_tool = CASE
                        WHEN excluded.best_tool = strategies.best_tool THEN strategies.best_tool
                        WHEN strategies.success_count > 5 THEN strategies.best_tool
                        ELSE excluded.best_tool END,
                    success_count = success_count + 1,
                    updated_at = excluded.updated_at
            """, (pattern, tool_name, tpl, time.time()))
            self._conn.commit()

    def _weaken(self, pattern, tool_name):
        with _db_lock:
            c = self._conn.cursor()
            c.execute("UPDATE strategies SET fail_count = fail_count + 1, updated_at = ? WHERE pattern = ? AND best_tool = ?",
                      (time.time(), pattern, tool_name))
            self._conn.commit()


# =====================================================================
# PHASE 2: CONTEXT COMPREHENDER — deep multi-turn understanding
# =====================================================================

class ContextComprehender:
    """Resolves pronouns, tracks referents, detects user sentiment.

    No DB tables — operates on in-memory conversation state.
    """

    def __init__(self):
        # Referent stack: what "it", "that", "this" currently mean
        self._referents = {}  # {"it": "Chrome", "that": "the file I created"}
        # Last N structured actions for reference resolution
        self._action_history = []  # [{tool, args, result, entities}]
        # Frustration detector
        self._correction_count = 0
        self._repeat_count = 0
        self._last_request = ""

    def update_referents(self, tool_name, arguments, result_text):
        """After a tool executes, update what pronouns refer to."""
        entry = {"tool": tool_name, "args": arguments, "result": str(result_text)[:150],
                 "time": time.time()}

        # Extract named entities from arguments
        if tool_name == "open_app":
            self._referents["it"] = arguments.get("name", "")
            self._referents["that"] = arguments.get("name", "")
            entry["entities"] = [arguments.get("name", "")]
        elif tool_name == "create_file":
            path = ""
            r = str(result_text)
            m = re.search(r'(?:created|saved|wrote)\s+(?:to\s+)?["\']?([^\s"\']+)', r, re.I)
            if m:
                path = m.group(1)
            self._referents["it"] = path or "the file"
            self._referents["that"] = path or "the file"
            self._referents["the file"] = path
            entry["entities"] = [path]
        elif tool_name == "google_search":
            self._referents["that"] = arguments.get("query", "")
            entry["entities"] = [arguments.get("query", "")]
        elif tool_name == "play_music":
            self._referents["it"] = arguments.get("query", "the music")
            self._referents["that"] = arguments.get("query", "the music")
            entry["entities"] = [arguments.get("query", "")]
        elif tool_name in ("manage_files", "manage_software", "run_terminal"):
            # For file/software ops, "it" = the path/name acted on
            target = arguments.get("path") or arguments.get("name") or arguments.get("command", "")
            if target:
                self._referents["it"] = target
                self._referents["that"] = target
            entry["entities"] = [target]

        self._action_history.append(entry)
        if len(self._action_history) > 10:
            self._action_history.pop(0)

    def resolve_pronouns(self, text):
        """Replace pronouns with their referents if unambiguous.

        'open it' → 'open Chrome' (if Chrome was last opened)
        'delete that' → 'delete report.pdf' (if that was last file)
        """
        if not self._referents:
            return text

        lower = text.lower()
        resolved = text

        # Only resolve when pronoun is in an actionable position
        # (after a verb, not in a question like "what is it")
        action_verbs = r"(open|close|delete|move|copy|rename|play|send|search|install|uninstall|run|show|find)"
        for pronoun, referent in self._referents.items():
            if not referent:
                continue
            pattern = rf'\b{action_verbs}\s+{re.escape(pronoun)}\b'
            if re.search(pattern, lower, re.I):
                resolved = re.sub(rf'\b{re.escape(pronoun)}\b', referent, resolved,
                                  count=1, flags=re.I)
                logger.info(f"Pronoun resolved: '{pronoun}' → '{referent}' in '{text}' → '{resolved}'")
                break

        return resolved

    def detect_frustration(self, text):
        """Detect if the user is getting frustrated (repeated corrections, same request).

        Returns: 'calm', 'mild', 'frustrated'
        """
        lower = text.lower().strip()

        # Correction pattern: "no", "I said", "not that", "wrong"
        if re.search(r'\b(no[,.]?\s|wrong|not that|i said|i meant|that\'s not)\b', lower, re.I):
            self._correction_count += 1
        else:
            self._correction_count = max(0, self._correction_count - 1)

        # Repetition: same request again
        if lower == self._last_request and lower:
            self._repeat_count += 1
        else:
            self._repeat_count = 0
        self._last_request = lower

        total = self._correction_count + self._repeat_count
        if total >= 3:
            return "frustrated"
        elif total >= 1:
            return "mild"
        return "calm"

    def get_context_summary(self):
        """Short summary of recent actions for LLM context."""
        if not self._action_history:
            return ""
        last = self._action_history[-1]
        entities = last.get("entities", [])
        entity_str = ", ".join(e for e in entities if e) if entities else ""
        return f"Last action: {last['tool']}({entity_str}). Pronouns: it={self._referents.get('it', '?')}, that={self._referents.get('that', '?')}"


# =====================================================================
# PHASE 3: PROBLEM SOLVER — decomposes complex goals into DAG steps
# =====================================================================
#
# Enhanced with JARVIS-style dependency DAG: steps have explicit deps,
# independent steps can run in parallel, and TAKEOVER points are marked
# for user intervention (login, payment, CAPTCHA).
# =====================================================================

# Task complexity classifications
COMPLEXITY_SIMPLE = "simple"       # Single tool call
COMPLEXITY_COMPOUND = "compound"   # Multiple independent steps (regex split)
COMPLEXITY_COMPLEX = "complex"     # Multi-step with dependencies (DAG)
COMPLEXITY_CREATIVE = "creative"   # Requires research + generation + verification


class TaskStep:
    """A single step in a task DAG.

    Attributes:
        id: Unique step ID (1-based)
        description: What this step does
        deps: List of step IDs this depends on (empty = no deps = can run early)
        tool_hint: Suggested tool name, or None
        takeover: If True, agent pauses for user (login, payment, etc.)
        status: "pending", "running", "done", "failed", "skipped"
        result: Execution result string
    """
    __slots__ = ("id", "description", "deps", "tool_hint",
                 "takeover", "status", "result")

    def __init__(self, id, description, deps=None, tool_hint=None, takeover=False):
        self.id = id
        self.description = description
        self.deps = deps or []
        self.tool_hint = tool_hint
        self.takeover = takeover
        self.status = "pending"
        self.result = ""

    def to_dict(self):
        return {
            "id": self.id, "step": self.description,
            "deps": self.deps, "tool_hint": self.tool_hint,
            "takeover": self.takeover, "status": self.status,
        }


class TaskDAG:
    """Dependency-aware task graph (inspired by Microsoft JARVIS).

    Steps with no dependencies can execute in parallel.
    Steps with deps wait until all dependencies are done.
    TAKEOVER steps pause for user intervention.
    """
    def __init__(self, steps=None):
        self.steps = steps or []  # List[TaskStep]

    def add_step(self, description, deps=None, tool_hint=None, takeover=False):
        step_id = len(self.steps) + 1
        step = TaskStep(step_id, description, deps, tool_hint, takeover)
        self.steps.append(step)
        return step

    def get_ready_steps(self):
        """Get steps whose dependencies are all satisfied (done/skipped).

        Returns list of TaskStep that can execute now.
        """
        done_ids = {s.id for s in self.steps if s.status in ("done", "skipped")}
        ready = []
        for s in self.steps:
            if s.status != "pending":
                continue
            if all(d in done_ids for d in s.deps):
                ready.append(s)
        return ready

    def get_next_step(self):
        """Get the single next step in topological order.

        For sequential execution (when parallelism isn't available).
        """
        ready = self.get_ready_steps()
        return ready[0] if ready else None

    def is_complete(self):
        """Are all steps done or skipped?"""
        return all(s.status in ("done", "skipped", "failed") for s in self.steps)

    def mark_done(self, step_id, result=""):
        for s in self.steps:
            if s.id == step_id:
                s.status = "done"
                s.result = result
                return

    def mark_failed(self, step_id, result=""):
        for s in self.steps:
            if s.id == step_id:
                s.status = "failed"
                s.result = result
                return

    def to_flat_list(self):
        """Convert to flat step list for backward compatibility.

        Returns: list of {"step": str, "tool_hint": str or None}
        """
        return [{"step": s.description, "tool_hint": s.tool_hint,
                 "deps": s.deps, "takeover": s.takeover}
                for s in self.steps]

    def to_plan_strings(self):
        """Convert to simple string list for desktop_agent plan format."""
        return [s.description for s in self.steps]

    def summary(self):
        """Human-readable DAG summary."""
        lines = []
        for s in self.steps:
            dep_str = f" (after step {','.join(map(str, s.deps))})" if s.deps else ""
            take_str = " [USER TAKEOVER]" if s.takeover else ""
            lines.append(f"  [{s.id}] {s.description}{dep_str}{take_str} [{s.status}]")
        return "\n".join(lines)


# TAKEOVER keywords — agent pauses for user on these
_TAKEOVER_KEYWORDS = [
    "password", "login", "sign in", "log in", "payment", "checkout",
    "credit card", "captcha", "verification code", "2fa", "two factor",
    "authenticate",
]


class ProblemSolver:
    """Breaks complex requests into executable sub-goals.

    Enhanced with:
    - Task complexity classification (simple/compound/complex/creative)
    - JARVIS-style dependency DAG for complex tasks
    - Skill library lookup before decomposition
    - TAKEOVER point detection for user intervention
    """

    def __init__(self, learner):
        self._learner = learner
        self._skill_lib = None  # Lazy-loaded

    def _get_skill_lib(self):
        """Lazy-load skill library."""
        if self._skill_lib is None:
            try:
                from skills import SkillLibrary
                self._skill_lib = SkillLibrary()
            except Exception as e:
                logger.debug(f"Skill library not available: {e}")
        return self._skill_lib

    def classify_complexity(self, text):
        """Classify task complexity level.

        Returns: COMPLEXITY_SIMPLE | COMPLEXITY_COMPOUND | COMPLEXITY_COMPLEX | COMPLEXITY_CREATIVE
        """
        lower = text.lower()

        # Creative tasks: require research + generation + verification
        creative_patterns = [
            r'\b(create|build|make|design|write|generate).{2,}(web\s*page|website|app|script|program|presentation|report|page)',
            r'\b(build|create|set up|make).{2,}(and|then).{2,}(host|deploy|publish|share)',
            r'\b(research|investigate|analyze).{2,}(and|then).{2,}(create|write|build|present)',
            r'\b(create|build|make).{2,}(about|for|on)\b.{2,}(and|then)',
        ]
        for p in creative_patterns:
            if re.search(p, lower):
                return COMPLEXITY_CREATIVE

        # Complex tasks: multi-step with dependencies (ordering, workflows)
        complex_patterns = [
            r'\b(order|buy|purchase|book|reserve|schedule).{5,}(from|on|at)\b',
            r'\b(set up|configure|install|deploy).{5,}(environment|server|database|project)',
            r'\b(download|install).{5,}(and|then).{5,}(configure|set up|run)',
            r'\b(find|search).{5,}(and|then).{5,}(download|install|open|save)',
            r'\b(back up|migrate|transfer|sync).{5,}(all|everything|files|data)',
            r'\b(automate|schedule|set up recurring)',
        ]
        for p in complex_patterns:
            if re.search(p, lower):
                return COMPLEXITY_COMPLEX

        # Compound: multiple independent actions joined by "and"
        compound_patterns = [
            r'\b(and then|then|after that|and also)\b',
            r'\b\w+\s+\w+\s+and\s+(open|play|search|close|create|install|delete|move|run)\b',
            r'\b(open|close|launch|start|play)\s+\w+\s+and\s+\w+',
        ]
        for p in compound_patterns:
            if re.search(p, lower):
                return COMPLEXITY_COMPOUND

        return COMPLEXITY_SIMPLE

    def needs_decomposition(self, text):
        """Does this request require multi-step decomposition?"""
        complexity = self.classify_complexity(text)
        return complexity != COMPLEXITY_SIMPLE

    def decompose(self, goal, available_tools):
        """Break a complex goal into ordered sub-tasks.

        Returns: list of {"step": str, "tool_hint": str or None, "deps": list, "takeover": bool}
        For backward compat, "deps" and "takeover" are optional keys.
        """
        complexity = self.classify_complexity(goal)

        # Check skill library first
        skill_lib = self._get_skill_lib()
        if skill_lib:
            matches = skill_lib.find_skill(goal, min_similarity=0.6, limit=1)
            if matches:
                match = matches[0]
                logger.info(f"Skill match: {match['name']} (similarity={match['similarity']:.2f})")
                # Convert skill's tool_sequence to step format
                steps = []
                for i, tool_call in enumerate(match["tool_sequence"]):
                    steps.append({
                        "step": tool_call.get("description", f"Step {i+1}: {tool_call.get('tool', '?')}"),
                        "tool_hint": tool_call.get("tool"),
                        "deps": [i] if i > 0 else [],
                        "takeover": False,
                        "from_skill": match["name"],
                    })
                if steps:
                    return steps

        # Simple compound: regex split
        if complexity == COMPLEXITY_COMPOUND:
            return self._decompose_compound(goal)

        # Complex/Creative: build DAG via LLM
        if complexity in (COMPLEXITY_COMPLEX, COMPLEXITY_CREATIVE):
            dag = self._decompose_dag(goal, available_tools, complexity)
            if dag and dag.steps:
                return dag.to_flat_list()

        # Fallback: single step
        return [{"step": goal, "tool_hint": None}]

    def decompose_to_dag(self, goal, available_tools):
        """Decompose into a TaskDAG object (for advanced callers).

        Returns: TaskDAG or None
        """
        complexity = self.classify_complexity(goal)
        if complexity in (COMPLEXITY_COMPLEX, COMPLEXITY_CREATIVE):
            return self._decompose_dag(goal, available_tools, complexity)
        # Wrap simple/compound as DAG
        steps = self.decompose(goal, available_tools)
        dag = TaskDAG()
        for i, step_dict in enumerate(steps):
            dag.add_step(
                step_dict["step"],
                deps=step_dict.get("deps", [i] if i > 0 else []),
                tool_hint=step_dict.get("tool_hint"),
                takeover=step_dict.get("takeover", False),
            )
        return dag

    def _decompose_compound(self, goal):
        """Split compound commands by conjunctions."""
        parts = re.split(r'\s+(?:and then|then|and also|after that|and)\s+', goal, flags=re.I)
        steps = []
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            tool_hint = None
            p = extract_pattern(part)
            if p != "general_chat":
                tool_hint = p
            steps.append({
                "step": part,
                "tool_hint": tool_hint,
                "deps": [i] if i > 0 else [],
                "takeover": False,
            })
        return steps if steps else [{"step": goal, "tool_hint": None}]

    def _decompose_dag(self, goal, available_tools, complexity):
        """Build a dependency DAG via LLM for complex/creative tasks.

        Uses JARVIS-style dependency specification.
        """
        extra_instruction = ""
        if complexity == COMPLEXITY_CREATIVE:
            extra_instruction = (
                "This is a CREATIVE task. Include a research step first, "
                "then a creation step, then a verification step.\n"
            )

        prompt = (
            f"Decompose this task into 3-8 steps with dependencies.\n"
            f"{extra_instruction}"
            f"TASK: {goal}\n"
            f"Available tools: {', '.join(available_tools[:20])}\n\n"
            f"Reply as a JSON array of objects. Each object has:\n"
            f'  "id": step number (1,2,3...)\n'
            f'  "step": description of what to do\n'
            f'  "deps": array of step IDs this depends on ([] = no deps)\n'
            f'  "tool": suggested tool name or null\n'
            f'  "takeover": true if user must handle this (login/payment/captcha)\n\n'
            f"Example:\n"
            f'[{{"id":1,"step":"Open Chrome browser","deps":[],"tool":"open_app","takeover":false}},\n'
            f' {{"id":2,"step":"Navigate to dominos.com","deps":[1],"tool":"google_search","takeover":false}},\n'
            f' {{"id":3,"step":"Complete payment","deps":[2],"tool":null,"takeover":true}}]\n\n'
            f"Reply ONLY with the JSON array."
        )

        resp = _llm_call(prompt, max_tokens=400)
        if not resp:
            return None

        try:
            # Extract JSON array
            m = re.search(r'\[.+\]', resp, re.DOTALL)
            if not m:
                return None

            steps_raw = json.loads(m.group(0))
            if not isinstance(steps_raw, list):
                return None

            dag = TaskDAG()
            valid_ids = set()

            for item in steps_raw:
                if not isinstance(item, dict):
                    continue

                step_id = item.get("id", len(dag.steps) + 1)
                desc = item.get("step", "")
                if not desc:
                    continue

                deps = item.get("deps", [])
                # Validate deps reference existing steps
                deps = [d for d in deps if isinstance(d, int) and d in valid_ids]

                tool_hint = item.get("tool")
                takeover = bool(item.get("takeover", False))

                # Auto-detect takeover from description
                if not takeover:
                    desc_lower = desc.lower()
                    for kw in _TAKEOVER_KEYWORDS:
                        if kw in desc_lower:
                            takeover = True
                            break

                step = dag.add_step(desc, deps=deps, tool_hint=tool_hint, takeover=takeover)
                # Override step.id to match LLM's numbering
                step.id = step_id
                valid_ids.add(step_id)

            if dag.steps:
                logger.info(f"DAG decomposition: {len(dag.steps)} steps\n{dag.summary()}")
                return dag

        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"DAG decomposition JSON parse failed: {e}")

        return None

    def find_alternative(self, failed_tool, goal):
        """When a tool fails, suggest an alternative approach.

        Returns: {"tool": str, "reason": str} or None
        """
        hint = self._learner.get_failure_hint(failed_tool) if self._learner else None
        if hint and hint.get("tool"):
            return {"tool": hint["tool"], "reason": hint["description"]}

        # Hardcoded fallback alternatives
        alternatives = {
            "manage_software": {"tool": "run_terminal", "reason": "Try terminal command instead"},
            "open_app": {"tool": "run_terminal", "reason": "Try 'start appname' in PowerShell"},
            "google_search": {"tool": "web_search_answer", "reason": "Try deep web search"},
            "play_music": {"tool": "open_app", "reason": "Open the music app directly"},
            "click_at": {"tool": "click_control", "reason": "Try UI Automation click instead"},
            "click_control": {"tool": "press_key", "reason": "Try keyboard shortcut instead"},
            "browser_action": {"tool": "click_control", "reason": "Try UI Automation instead"},
            "type_text": {"tool": "press_key", "reason": "Try pressing keys individually"},
        }
        return alternatives.get(failed_tool)


# =====================================================================
# PHASE 4: DECISION ENGINE — confidence-weighted tool selection
# =====================================================================

class DecisionEngine:
    """Estimates confidence before acting. Asks user when uncertain."""

    def __init__(self, learner):
        self._learner = learner
        # Risk classification for each tool
        self._risk_levels = {
            "open_app": "safe", "close_app": "safe", "minimize_app": "safe",
            "get_weather": "safe", "get_forecast": "safe", "get_time": "safe",
            "get_news": "safe", "google_search": "safe", "play_music": "safe",
            "set_reminder": "safe", "list_reminders": "safe",
            "toggle_setting": "moderate", "create_file": "moderate",
            "run_terminal": "moderate", "manage_files": "moderate",
            "manage_software": "moderate", "send_email": "risky",
            "system_command": "risky", "agent_task": "risky",
        }

    def get_confidence(self, request_text, proposed_tool):
        """Estimate P(success) for using this tool on this request.

        Returns: float 0.0-1.0
        """
        if not self._learner:
            return 0.7  # Default when no data

        # Base confidence from experience
        stats = self._learner.get_success_rate(proposed_tool)
        if stats["total"] == 0:
            return 0.6  # No data — moderate confidence

        base = stats["rate"]

        # Pattern-specific boost: does experience say this tool works for this pattern?
        strategy = self._learner.get_strategy(request_text)
        if strategy and strategy["tool"] == proposed_tool:
            base = max(base, strategy["confidence"])
        elif strategy and strategy["tool"] != proposed_tool:
            base *= 0.7  # Penalty: experience suggests a different tool

        # Risk adjustment
        risk = self._risk_levels.get(proposed_tool, "moderate")
        if risk == "risky":
            base *= 0.8  # Extra caution for risky tools

        return min(base, 1.0)

    def should_confirm(self, request_text, proposed_tool, confidence):
        """Should we ask the user before acting?

        Returns: (needs_confirm: bool, reason: str or None)
        """
        risk = self._risk_levels.get(proposed_tool, "moderate")

        # Always confirm risky actions with low confidence
        if risk == "risky" and confidence < 0.7:
            return True, f"I'm not very confident about {proposed_tool}. Want me to proceed?"

        # Confirm moderate-risk actions with very low confidence
        if risk == "moderate" and confidence < 0.4:
            return True, f"This might not work as expected. Should I try {proposed_tool}?"

        # Confirm when experience says this tool usually fails for this pattern
        if confidence < 0.3:
            alt = self._get_alternative_suggestion(request_text, proposed_tool)
            if alt:
                return True, f"Based on my experience, {alt} might work better. Want me to try that instead?"

        return False, None

    def _get_alternative_suggestion(self, request_text, proposed_tool):
        """Suggest a better tool based on experience."""
        strategy = self._learner.get_strategy(request_text) if self._learner else None
        if strategy and strategy["tool"] != proposed_tool and strategy["confidence"] > 0.6:
            return strategy["tool"]
        return None


# =====================================================================
# PHASE 5: CREATIVE ENGINE — novel solutions and proactive suggestions
# =====================================================================

class CreativeEngine:
    """Generates creative suggestions and novel approaches.

    Uses LLM for ideation, experience data for personalization.
    """

    def __init__(self, conn, learner):
        self._conn = conn
        self._learner = learner
        self._init_tables()

    def _init_tables(self):
        c = self._conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS creative_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion TEXT NOT NULL,
                category TEXT NOT NULL,
                accepted INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def get_proactive_suggestion(self, hour=None):
        """Context-aware suggestion based on time and usage patterns."""
        now = datetime.now()
        h = hour or now.hour

        # Time-based suggestions
        if 6 <= h <= 9:
            suggestions = [
                "Want me to check your reminders and the weather?",
                "Shall I open your usual morning apps?",
            ]
        elif 12 <= h <= 13:
            suggestions = [
                "It's lunchtime. Want me to play some music?",
            ]
        elif 17 <= h <= 19:
            suggestions = [
                "End of workday? Want me to organize your Downloads folder?",
            ]
        elif h >= 22:
            suggestions = [
                "Getting late. Want me to enable night light?",
            ]
        else:
            suggestions = []

        # Usage-pattern based suggestions
        if self._learner:
            stats = self._learner.get_success_rate()
            if stats["total"] >= 20 and stats["rate"] < 0.7:
                suggestions.append("I've been having some trouble lately. Want me to analyze what's going wrong?")

            failures = self._learner.get_top_failures(1)
            if failures and failures[0]["cnt"] >= 5:
                f = failures[0]
                # Avoid redundant "X for X" when pattern matches tool name
                if f['request_pattern'] == f['tool_name'] or f['request_pattern'] in ("unknown", "general_chat"):
                    suggestions.append(f"I keep having issues with {f['tool_name']}. Want me to find a better approach?")
                else:
                    suggestions.append(f"I keep having issues with {f['tool_name']} for '{f['request_pattern']}' requests. "
                                       f"Want me to find a better approach?")

        return suggestions[0] if suggestions else None

    def generate_creative_response(self, request_text):
        """For vague/creative requests, generate a novel solution idea.

        'make me something cool' → specific project idea
        'surprise me' → unexpected but useful action
        """
        prompt = (
            f"The user said: '{request_text}'\n"
            f"Suggest ONE specific, practical thing to create or do on their Windows computer.\n"
            f"Be creative but realistic. Available tools: file creation, terminal commands, "
            f"music playback, web search, app management.\n"
            f"Reply in one short sentence describing what you'll do."
        )
        return _llm_call(prompt, max_tokens=80, temperature=0.9)

    def log_suggestion_response(self, suggestion, accepted):
        """Track whether user accepted or rejected a suggestion."""
        with _db_lock:
            c = self._conn.cursor()
            col = "accepted" if accepted else "rejected"
            c.execute(f"""
                UPDATE creative_suggestions SET {col} = {col} + 1
                WHERE suggestion = ? ORDER BY created_at DESC LIMIT 1
            """, (suggestion,))
            if c.rowcount == 0:
                c.execute("""
                    INSERT INTO creative_suggestions (suggestion, category, accepted, rejected, created_at)
                    VALUES (?, 'proactive', ?, ?, ?)
                """, (suggestion, 1 if accepted else 0, 0 if accepted else 1, time.time()))
            self._conn.commit()


# =====================================================================
# PHASE 6: AUTONOMY ENGINE — self-directed improvement
# =====================================================================

class AutonomyEngine:
    """Analyzes own performance and proposes improvements.

    Runs periodically (not on every request) to avoid overhead.
    """

    def __init__(self, conn, learner):
        self._conn = conn
        self._learner = learner
        self._last_analysis_time = 0
        self._analysis_interval = 3600  # Analyze every hour at most
        self._init_tables()
        # Budget-constrained self-improvement
        from cognitive_budget import BudgetedRunner, ImprovementBudget, ImprovementTracker
        self._budget_runner = BudgetedRunner(ImprovementBudget())
        self._improvement_tracker = ImprovementTracker(review_window_seconds=3600)

    def _init_tables(self):
        c = self._conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS self_improvements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'proposed',
                created_at REAL NOT NULL,
                applied_at REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS prompt_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_pattern TEXT NOT NULL UNIQUE,
                adjustment TEXT NOT NULL,
                reason TEXT,
                effectiveness REAL DEFAULT 0.0,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def maybe_analyze(self):
        """Run self-analysis if enough time has passed. Returns insights or None."""
        now = time.time()
        if now - self._last_analysis_time < self._analysis_interval:
            return None
        self._last_analysis_time = now
        return self._analyze()

    def _analyze(self):
        """Analyze recent performance and generate improvement proposals.

        Enhanced with:
        - Auto-generate prompt adjustments for high-failure patterns
        - Web research for solutions to recurring problems
        - Skill cleanup (remove consistently failing skills)
        - Time-budgeted execution with auto-revert of bad improvements
        """
        if not self._learner:
            return None

        self._budget_runner.start()
        logger.info("Starting budgeted improvement cycle")

        insights = []

        # 1. Find consistently failing tools
        stats = self._learner.get_success_rate(days=3)
        if stats["total"] < 5:
            return None  # Not enough data

        if stats["rate"] < 0.6:
            insights.append({
                "type": "low_success_rate",
                "detail": f"Overall success rate is {stats['rate']:.0%} over 3 days",
                "suggestion": "Review failure patterns and add recovery strategies",
            })

        # 2. Find tools that fail more than 40% of the time
        weak_tools = []
        with _db_lock:
            c = self._conn.cursor()
            cutoff = time.time() - (3 * 86400)
            c.execute("""
                SELECT tool_name,
                    SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) as ok,
                    SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) as fail,
                    COUNT(*) as total
                FROM experience_log WHERE timestamp > ?
                GROUP BY tool_name HAVING total >= 3
            """, (cutoff,))
            for row in c.fetchall():
                rate = row["ok"] / row["total"] if row["total"] > 0 else 1.0
                if rate < 0.6:
                    insights.append({
                        "type": "weak_tool",
                        "detail": f"{row['tool_name']} fails {row['fail']}/{row['total']} times",
                        "suggestion": f"Add prompt hints or fallback for {row['tool_name']}",
                    })
                    weak_tools.append(row["tool_name"])

        # 3. Find repeated request patterns that always use the same failing approach
        failures = self._learner.get_top_failures(3, days=3)
        for f in failures:
            if f["cnt"] >= 3:
                insights.append({
                    "type": "repeated_failure",
                    "detail": f"{f['tool_name']} fails {f['cnt']}x for '{f['request_pattern']}'",
                    "suggestion": f"Learn alternative approach for '{f['request_pattern']}'",
                })

        # 4. AUTO-GENERATE prompt adjustments for weak patterns (budget-constrained)
        for f in failures:
            if f["cnt"] >= 3:
                if not self._budget_runner.can_improve():
                    logger.info("Budget exhausted: max improvements reached")
                    break
                pattern = f["request_pattern"]
                tool = f["tool_name"]
                # Generate a prompt hint to avoid this failure
                adjustment = f"IMPORTANT: {tool} often fails for '{pattern}' requests. "
                # Check if there's a known fix
                fix = self._learner.get_failure_hint(tool)
                if fix and fix.get("tool"):
                    adjustment += f"Try {fix['tool']} instead. "
                else:
                    adjustment += f"Use a different tool or approach. "
                self.learn_prompt_adjustment(pattern, adjustment,
                                            reason=f"Auto-fix: {tool} fails {f['cnt']}x")
                self._budget_runner.track_improvement()
                # Record for later review
                stats = self._learner.get_success_rate(days=3)
                self._improvement_tracker.record_improvement(
                    pattern, f"auto_fix_{tool}_{pattern}",
                    stats["rate"], f"Auto-fix: {tool} fails {f['cnt']}x"
                )
                logger.info(f"Auto-tuned prompt for '{pattern}': {adjustment[:60]}")

        # 5. SKILL CLEANUP — remove consistently failing skills
        try:
            from skills import SkillLibrary
            skill_lib = SkillLibrary()
            skill_lib.cleanup(max_age_days=60, min_success=1)
        except Exception:
            pass

        # 6. WEB RESEARCH for top failing patterns (budget-constrained)
        if failures and len(failures) > 0:
            worst = failures[0]
            if worst["cnt"] >= 5:
                if self._budget_runner.can_web():
                    self._budget_runner.track_web()
                    self._research_improvement(
                        worst["tool_name"], worst["request_pattern"], worst["cnt"]
                    )
                else:
                    logger.info("Budget exhausted: no more web calls allowed")

        # Store proposals
        for insight in insights:
            self._store_proposal(insight)

        # Review past improvements — auto-revert bad ones
        try:
            reviews = self._improvement_tracker.review_pending(self._get_success_rate)
            for review in reviews:
                if review["action"] == "revert":
                    self._revert_improvement(review["adjustment_id"])
        except Exception as e:
            logger.warning(f"Improvement review failed: {e}")

        logger.info(f"Improvement cycle complete: {self._budget_runner.summary()}")

        return insights if insights else None

    def _research_improvement(self, tool_name, pattern, failure_count):
        """Research improvements online for consistently failing patterns.

        Runs during self-analysis (hourly max), not on every request.
        Budget-constrained: checks LLM and improvement limits before proceeding.
        """
        try:
            if not self._budget_runner.can_llm():
                logger.info("Budget exhausted: no more LLM calls allowed")
                return
            self._budget_runner.track_llm()

            from web_agent import research_solution

            solution = research_solution(
                goal=f"fix {tool_name} failures for '{pattern}' tasks",
                error_message=f"{tool_name} fails {failure_count} times for '{pattern}'",
                llm_fn=_llm_call,
            )

            if solution and solution.get("confidence", 0) > 0.3:
                if not self._budget_runner.can_improve():
                    logger.info("Budget exhausted: max improvements reached")
                    return
                self._budget_runner.track_improvement()

                # Store as a prompt adjustment
                steps_str = "; ".join(solution.get("steps", [])[:2])
                adjustment = f"Research found: {solution.get('solution', '')[:80]}. Try: {steps_str}"
                self.learn_prompt_adjustment(
                    pattern, adjustment[:200],
                    reason=f"Web research for {tool_name} failures"
                )
                logger.info(f"Research-based improvement for {pattern}: {adjustment[:80]}")

                # Record for later review
                stats = self._learner.get_success_rate(days=3)
                self._improvement_tracker.record_improvement(
                    pattern, f"research_{tool_name}_{pattern}",
                    stats["rate"], f"Web research fix for {tool_name}"
                )

                # Store as self-improvement proposal
                self._store_proposal({
                    "type": "research_solution",
                    "detail": f"Found fix for {tool_name}/{pattern}: {solution.get('solution', '')[:100]}",
                    "suggestion": adjustment[:200],
                })

        except Exception as e:
            logger.debug(f"Research improvement failed: {e}")

    def get_prompt_adjustment(self, request_text):
        """Check if there's a learned prompt adjustment for this request pattern."""
        pattern = extract_pattern(request_text)
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT adjustment FROM prompt_adjustments
                WHERE trigger_pattern = ? AND effectiveness > 0
                ORDER BY effectiveness DESC LIMIT 1
            """, (pattern,))
            row = c.fetchone()
        return row["adjustment"] if row else None

    def learn_prompt_adjustment(self, pattern, adjustment, reason="auto-learned"):
        """Store a prompt adjustment that improved results for a pattern."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT INTO prompt_adjustments (trigger_pattern, adjustment, reason, effectiveness, created_at)
                VALUES (?, ?, ?, 0.5, ?)
                ON CONFLICT(trigger_pattern) DO UPDATE SET
                    adjustment = excluded.adjustment,
                    effectiveness = MIN(effectiveness + 0.1, 1.0),
                    created_at = excluded.created_at
            """, (pattern, adjustment, reason, time.time()))
            self._conn.commit()

    def get_improvement_proposals(self, status="proposed"):
        """Get pending improvement proposals."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT category, description, created_at FROM self_improvements
                WHERE status = ? ORDER BY created_at DESC LIMIT 5
            """, (status,))
            return [dict(row) for row in c.fetchall()]

    def generate_self_report(self):
        """Human-readable report of cognitive state."""
        if not self._learner:
            return "Cognitive engine has no data yet."

        stats = self._learner.get_success_rate()
        parts = [f"I've handled {stats['total']} tasks with a {stats['rate']:.0%} success rate."]

        with _db_lock:
            c = self._conn.cursor()
            c.execute("SELECT COUNT(*) as n FROM strategies WHERE success_count > fail_count")
            n = c.fetchone()["n"]
            if n:
                parts.append(f"I know {n} proven strategies.")

            c.execute("SELECT COUNT(*) as n FROM failure_lessons WHERE occurrences >= 2")
            n = c.fetchone()["n"]
            if n:
                parts.append(f"I've learned {n} lessons from failures.")

            c.execute("SELECT COUNT(*) as n FROM prompt_adjustments WHERE effectiveness > 0")
            n = c.fetchone()["n"]
            if n:
                parts.append(f"I've auto-tuned {n} prompt rules.")

            c.execute("SELECT COUNT(*) as n FROM self_improvements WHERE status = 'proposed'")
            n = c.fetchone()["n"]
            if n:
                parts.append(f"I have {n} self-improvement proposals pending.")

        failures = self._learner.get_top_failures(1)
        if failures:
            f = failures[0]
            parts.append(f"Weakest area: {f['tool_name']} for '{f['request_pattern']}' ({f['cnt']} failures).")

        return " ".join(parts)

    def _get_success_rate(self, pattern):
        """Get success rate for a pattern from experience log."""
        try:
            stats = self._learner.get_success_rate(days=3)
            return stats.get("rate", 0.5)
        except Exception:
            return 0.5

    def _revert_improvement(self, adjustment_id):
        """Revert a bad improvement by removing its prompt adjustment."""
        try:
            if self._conn:
                with _db_lock:
                    # adjustment_id format: "auto_fix_{tool}_{pattern}" or "research_{tool}_{pattern}"
                    # Extract the pattern part to find the matching prompt_adjustment
                    parts = adjustment_id.split("_", 2)
                    if len(parts) >= 3:
                        # Try to match by trigger_pattern
                        pattern = parts[-1]
                        self._conn.execute(
                            "DELETE FROM prompt_adjustments WHERE trigger_pattern = ?",
                            (pattern,)
                        )
                    else:
                        # Fallback: try direct match
                        self._conn.execute(
                            "DELETE FROM prompt_adjustments WHERE trigger_pattern = ?",
                            (adjustment_id,)
                        )
                    self._conn.commit()
                    logger.info(f"Reverted improvement: {adjustment_id}")
        except Exception as e:
            logger.warning(f"Failed to revert improvement {adjustment_id}: {e}")

    def _store_proposal(self, insight):
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO self_improvements (category, description, created_at)
                VALUES (?, ?, ?)
            """, (insight["type"], insight["suggestion"], time.time()))
            self._conn.commit()


# =====================================================================
# COORDINATOR: CognitiveEngine — single entry point for brain.py
# =====================================================================

class CognitiveEngine:
    """Orchestrates all cognitive phases. Brain.py creates one instance.

    Usage in brain.py:
        self._cognition = CognitiveEngine()

        # After tool execution:
        self._cognition.log_outcome(request, tool, args, success, result)

        # Before LLM call (inject into system prompt):
        advice = self._cognition.get_context(user_input)

        # For complex requests:
        if self._cognition.needs_decomposition(text):
            steps = self._cognition.decompose(text, tool_names)

        # Confidence check:
        conf = self._cognition.get_confidence(text, tool_name)

        # Self-report:
        report = self._cognition.get_report()
    """

    def __init__(self, db_path=DB_FILE):
        self._conn = _get_db(db_path)

        # Initialize all phases
        self.learner = ExperienceLearner(self._conn)
        self.comprehender = ContextComprehender()
        self.solver = ProblemSolver(self.learner)
        self.decider = DecisionEngine(self.learner)
        self.creative = CreativeEngine(self._conn, self.learner)
        self.autonomy = AutonomyEngine(self._conn, self.learner)

    # --- Phase 1: Learning ---

    def log_outcome(self, request_text, tool_name, arguments, success, result_text=""):
        """Log every tool execution outcome."""
        self.learner.log_outcome(request_text, tool_name, arguments, success, result_text)
        # Also update comprehender's referent tracking
        self.comprehender.update_referents(tool_name, arguments or {}, result_text)

    def log_recovery(self, request_text, original_tool, recovery_tool, recovery_args):
        """Log when an alternative approach fixed a failure."""
        self.learner.log_recovery(request_text, original_tool, recovery_tool, recovery_args)

    # --- Phase 2: Comprehension ---

    def resolve_input(self, text):
        """Resolve pronouns and references in user input.

        'open it' → 'open Chrome'
        'delete that' → 'delete report.pdf'
        """
        return self.comprehender.resolve_pronouns(text)

    def get_frustration_level(self, text):
        """Detect user frustration: 'calm', 'mild', 'frustrated'."""
        return self.comprehender.detect_frustration(text)

    # --- Phase 3: Problem Solving ---

    def needs_decomposition(self, text):
        """Does this request need to be broken into sub-steps?"""
        return self.solver.needs_decomposition(text)

    def decompose(self, text, available_tools):
        """Break complex request into ordered sub-tasks."""
        return self.solver.decompose(text, available_tools)

    def decompose_to_dag(self, text, available_tools):
        """Break complex request into a TaskDAG with dependencies."""
        return self.solver.decompose_to_dag(text, available_tools)

    def classify_complexity(self, text):
        """Classify task complexity: simple/compound/complex/creative."""
        return self.solver.classify_complexity(text)

    def find_alternative(self, failed_tool, goal):
        """Suggest alternative when a tool fails."""
        return self.solver.find_alternative(failed_tool, goal)

    # --- Phase 4: Decision Making ---

    def get_confidence(self, request_text, proposed_tool):
        """Estimate P(success) for a tool choice. Returns 0.0-1.0."""
        return self.decider.get_confidence(request_text, proposed_tool)

    def should_confirm(self, request_text, proposed_tool):
        """Should we ask the user before acting?

        Returns: (needs_confirm, reason_message)
        """
        conf = self.get_confidence(request_text, proposed_tool)
        return self.decider.should_confirm(request_text, proposed_tool, conf)

    # --- Phase 5: Creativity ---

    def get_proactive_suggestion(self):
        """Context-aware suggestion for the user."""
        return self.creative.get_proactive_suggestion()

    def get_creative_response(self, text):
        """Generate a creative solution for vague requests."""
        return self.creative.generate_creative_response(text)

    # --- Phase 6: Autonomy ---

    def run_self_analysis(self):
        """Periodic self-improvement analysis. Returns insights or None."""
        return self.autonomy.maybe_analyze()

    def get_prompt_adjustment(self, request_text):
        """Get auto-learned prompt tweak for this request type."""
        return self.autonomy.get_prompt_adjustment(request_text)

    # --- Combined: context string for brain system prompt ---

    def get_context(self, user_input):
        """Build cognitive context string to inject into system prompt.

        Combines: learned strategies, failure warnings, comprehension state,
        prompt adjustments, frustration level.
        """
        parts = []

        # Phase 1: learned advice
        strategy = self.learner.get_strategy(user_input)
        if strategy:
            parts.append(f"Learned: {strategy['tool']} works well here (confidence: {strategy['confidence']:.0%})")

        lessons = self.learner.get_failure_lessons(2)
        for lesson in lessons:
            parts.append(f"Watch out: {lesson['tool_name']} sometimes fails — {lesson['fix_description']}")

        # Phase 2: comprehension context
        ctx = self.comprehender.get_context_summary()
        if ctx:
            parts.append(ctx)

        frustration = self.comprehender.detect_frustration(user_input)
        if frustration == "frustrated":
            parts.append("USER IS FRUSTRATED — be extra careful, confirm before acting, apologize if needed")
        elif frustration == "mild":
            parts.append("User may be getting impatient — be precise and efficient")

        # Phase 6: auto-tuned prompt adjustments
        adj = self.autonomy.get_prompt_adjustment(user_input)
        if adj:
            parts.append(f"Auto-rule: {adj}")

        return "\n".join(parts) if parts else ""

    # --- Report ---

    def get_report(self):
        """Full cognitive self-report."""
        return self.autonomy.generate_self_report()

    def close(self):
        with _db_lock:
            self._conn.close()
