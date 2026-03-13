"""
Skill Library — Voyager-inspired persistent skill storage and retrieval.

Stores successful multi-step tool sequences as reusable "skills" that can be
retrieved by semantic similarity when facing similar tasks. Skills compound:
complex skills compose from simpler ones.

Architecture:
  SkillLibrary
    ├── save_skill()      — store a successful tool sequence as a named skill
    ├── find_skill()      — retrieve best-matching skill for a new goal
    ├── execute_skill()   — replay a stored skill with parameter substitution
    ├── refine_skill()    — improve a skill based on execution feedback
    └── list_skills()     — get all skills for inspection

Storage: SQLite (memory.db) — same DB as cognitive engine.
Retrieval: TF-IDF keyword overlap (fast, no external deps).

Inspired by:
  - Voyager (Minecraft agent skill library)
  - Hermes Agent (persistent cross-session skills)
  - SICA (self-improving coding agent)
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter

try:
    from embeddings import VectorStore
    _HAS_VECTOR_STORE = True
except ImportError:
    _HAS_VECTOR_STORE = False

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")
_db_lock = threading.Lock()

# Stop words for TF-IDF similarity
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "i", "me", "my",
    "you", "your", "he", "she", "it", "we", "they", "this", "that",
    "and", "or", "but", "if", "then", "of", "in", "on", "at", "to",
    "for", "with", "from", "by", "about", "as", "into", "through",
    "please", "want", "need", "like", "just", "also", "very", "really",
})


def _tokenize(text):
    """Lowercase tokenization with stop word removal."""
    words = re.findall(r'[a-z][a-z0-9]+', text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


def _tfidf_similarity(query_tokens, doc_tokens):
    """Simple TF-IDF cosine similarity between two token lists."""
    if not query_tokens or not doc_tokens:
        return 0.0

    q_counts = Counter(query_tokens)
    d_counts = Counter(doc_tokens)
    all_terms = set(q_counts) | set(d_counts)

    # Compute dot product and magnitudes
    dot = 0.0
    q_mag = 0.0
    d_mag = 0.0
    for term in all_terms:
        q_val = q_counts.get(term, 0)
        d_val = d_counts.get(term, 0)
        dot += q_val * d_val
        q_mag += q_val * q_val
        d_mag += d_val * d_val

    if q_mag == 0 or d_mag == 0:
        return 0.0

    return dot / (math.sqrt(q_mag) * math.sqrt(d_mag))


class SkillLibrary:
    """Persistent skill library with semantic retrieval.

    Usage:
        lib = SkillLibrary()

        # After a successful multi-step task:
        lib.save_skill(
            name="order_pizza",
            description="Order pizza from Domino's website",
            goal="order me a pizza from dominos",
            tool_sequence=[
                {"tool": "open_app", "args": {"name": "Chrome"}, "result": "Opened Chrome"},
                {"tool": "google_search", "args": {"query": "dominos.com"}, "result": "Searched"},
                ...
            ],
            tags=["web", "food", "ordering"]
        )

        # Before starting a new task:
        match = lib.find_skill("order a pizza")
        if match and match["similarity"] > 0.7:
            steps = match["tool_sequence"]
    """

    def __init__(self, db_path=DB_FILE):
        self._db_path = db_path
        self._conn = self._get_db()
        self._init_tables()
        # Initialize vector store for semantic search
        self._vectors = None
        if _HAS_VECTOR_STORE:
            try:
                self._vectors = VectorStore(
                    persist_dir=os.path.dirname(os.path.abspath(db_path)),
                    prefix="skill_vectors",
                )
                logger.debug("SkillLibrary: VectorStore initialized")
            except Exception as e:
                logger.warning(f"SkillLibrary: VectorStore init failed: {e}")
                self._vectors = None
        # Auto-cleanup stale skills on load
        try:
            self.cleanup()
        except Exception as e:
            logger.debug(f"Skill auto-cleanup failed: {e}")

    def _get_db(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    tool_sequence TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    tokens TEXT NOT NULL,
                    success_count INTEGER DEFAULT 1,
                    fail_count INTEGER DEFAULT 0,
                    avg_duration REAL DEFAULT 0.0,
                    parent_skills TEXT DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS skill_reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    reflection TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (skill_name) REFERENCES skills(name)
                )
            """)

            # Schema migration — add structured metadata columns (Composio pattern)
            _migrations = [
                "ALTER TABLE skills ADD COLUMN activation_triggers TEXT DEFAULT '[]'",
                "ALTER TABLE skills ADD COLUMN required_credentials TEXT DEFAULT '[]'",
                "ALTER TABLE skills ADD COLUMN category TEXT DEFAULT 'general'",
                "ALTER TABLE skills ADD COLUMN expected_outputs TEXT DEFAULT '[]'",
                "ALTER TABLE skills ADD COLUMN version INTEGER DEFAULT 1",
                "ALTER TABLE skills ADD COLUMN parameters_template TEXT DEFAULT '{}'",
                "ALTER TABLE skills ADD COLUMN depends_on TEXT DEFAULT '[]'",
                "ALTER TABLE skills ADD COLUMN cooldown_seconds INTEGER DEFAULT 0",
            ]
            for sql in _migrations:
                try:
                    c.execute(sql)
                except Exception:
                    pass  # Column already exists

            self._conn.commit()

    # Browser process names that web-navigation tools inherently launch
    _BROWSER_APPS = frozenset({
        "chrome", "google chrome", "chromium",
        "firefox", "mozilla firefox",
        "edge", "microsoft edge",
        "opera", "brave", "vivaldi", "safari",
    })

    # Tools that open a browser as a side-effect
    _BROWSER_OPENING_TOOLS = frozenset({
        "google_search", "web_read", "web_search_answer",
    })

    # Tools that open the *named* app as a side-effect
    # Maps tool_name → arg key whose value is the app name
    _APP_OPENING_TOOLS = {
        "search_in_app": "app",          # search_in_app(app="Spotify", ...)
        "play_music": None,              # always opens Spotify / media player
    }

    # Stopwords for trigger extraction (broader than TF-IDF stopwords)
    _TRIGGER_STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "and", "or", "but", "not", "this",
        "that", "it", "its", "my", "your", "his", "her", "our", "their",
        "me", "you", "him", "them", "us", "what", "which", "who", "how",
        "when", "where", "why", "get", "set", "make", "let", "please",
    })

    # Tool-name → category mapping for auto-inference
    _WEB_TOOLS = frozenset({
        "web_read", "web_search", "web_search_answer", "google_search", "browser_action",
    })
    _SYSTEM_TOOLS = frozenset({
        "run_terminal", "manage_files", "manage_software", "system_command",
    })
    _COMM_TOOLS = frozenset({"send_email"})
    _AUTO_TOOLS = frozenset({
        "agent_task", "click_at", "type_text", "press_key", "scroll",
    })

    def _infer_category(self, tool_sequence):
        """Infer skill category from tool names in the sequence."""
        tool_names = {tc.get("tool", "") for tc in tool_sequence}

        if tool_names & self._WEB_TOOLS:
            return "web"
        if tool_names & self._AUTO_TOOLS:
            return "automation"
        if tool_names & self._SYSTEM_TOOLS:
            return "system"
        if tool_names & self._COMM_TOOLS:
            return "communication"
        return "general"

    def _extract_triggers(self, goal):
        """Extract activation trigger keywords from goal text."""
        words = re.findall(r'\b[a-z]{4,}\b', goal.lower())
        return [w for w in words if w not in self._TRIGGER_STOPWORDS][:5]

    @staticmethod
    def _deduplicate_steps(tool_sequence):
        """Remove redundant steps from a tool sequence before storage.

        Rules applied in order:
          1. open_app(<browser>) before a browser-opening tool  → drop open_app
          2. open_app(X) before search_in_app(app=X, ...)       → drop open_app
          3. open_app("spotify") before play_music               → drop open_app
          4. open_app(X) immediately before open_app(X)         → drop first
          5. Consecutive identical tool+args calls               → keep only first

        Args:
            tool_sequence: List of {"tool": str, "args": dict, ...} dicts.

        Returns:
            New list with redundant steps removed.
        """
        if not tool_sequence:
            return tool_sequence

        steps = list(tool_sequence)  # shallow copy so we don't mutate caller's list
        changed = True

        while changed:
            changed = False
            new_steps = []
            skip_next = False

            for i, step in enumerate(steps):
                if skip_next:
                    skip_next = False
                    continue

                tool = step.get("tool", "")
                args = step.get("args", {}) or {}

                # Look ahead at next step (if any)
                next_step = steps[i + 1] if i + 1 < len(steps) else None
                next_tool = next_step.get("tool", "") if next_step else ""
                next_args = next_step.get("args", {}) or {} if next_step else {}

                if tool == "open_app":
                    app_name = (args.get("name") or args.get("app") or "").lower().strip()

                    # Rule 1: open_app(<browser>) before a browser-opening tool
                    if (app_name in SkillLibrary._BROWSER_APPS
                            and next_tool in SkillLibrary._BROWSER_OPENING_TOOLS):
                        logger.debug(
                            f"Dedup: dropping open_app({app_name!r}) — "
                            f"{next_tool} opens browser implicitly"
                        )
                        changed = True
                        continue  # drop this open_app, keep next step as-is

                    # Rule 2: open_app(X) before search_in_app(app=X, ...)
                    if next_tool == "search_in_app":
                        search_app = (
                            next_args.get("app") or next_args.get("name") or ""
                        ).lower().strip()
                        if search_app and search_app == app_name:
                            logger.debug(
                                f"Dedup: dropping open_app({app_name!r}) — "
                                f"search_in_app opens it implicitly"
                            )
                            changed = True
                            continue

                    # Rule 3: open_app("spotify") before play_music
                    if app_name == "spotify" and next_tool == "play_music":
                        logger.debug(
                            "Dedup: dropping open_app('spotify') — "
                            "play_music opens Spotify implicitly"
                        )
                        changed = True
                        continue

                    # Rule 4: open_app(X) immediately before open_app(X)
                    if next_tool == "open_app":
                        next_app = (
                            next_args.get("name") or next_args.get("app") or ""
                        ).lower().strip()
                        if next_app == app_name:
                            logger.debug(
                                f"Dedup: dropping duplicate open_app({app_name!r})"
                            )
                            changed = True
                            continue  # drop first, keep second (next iteration)

                # Rule 5: consecutive identical tool + args calls
                if next_step and tool == next_tool:
                    # Compare args ignoring result field
                    def _canon(a):
                        return json.dumps(
                            {k: v for k, v in sorted(a.items()) if k != "result"},
                            sort_keys=True
                        )
                    if _canon(args) == _canon(next_args):
                        logger.debug(
                            f"Dedup: dropping duplicate consecutive call to {tool!r}"
                        )
                        # Keep this one, skip the next
                        new_steps.append(step)
                        skip_next = True
                        changed = True
                        continue

                new_steps.append(step)

            steps = new_steps

        return steps

    def save_skill(self, name, description, goal, tool_sequence,
                   tags=None, parent_skills=None, duration=0.0,
                   activation_triggers=None, required_credentials=None,
                   category=None, expected_outputs=None, cooldown_seconds=0,
                   depends_on=None):
        """Store a successful tool sequence as a reusable skill.

        Args:
            name: Unique skill name (e.g. "order_pizza")
            description: What the skill does
            goal: The original user request that triggered this skill
            tool_sequence: List of {"tool": str, "args": dict, "result": str}
            tags: Optional list of category tags
            parent_skills: Optional list of sub-skill names this composes from
            duration: How long the skill took to execute (seconds)
            activation_triggers: Keywords/regex patterns that activate this skill
            required_credentials: Config keys needed (e.g. ["api_key", "smtp_user"])
            category: Skill category (web, system, communication, automation, general)
            expected_outputs: Descriptions of expected outputs
            cooldown_seconds: Minimum seconds between replays (0 = no cooldown)
            depends_on: Names of other skills this depends on

        Returns:
            str: Success/failure message
        """
        if not name or not tool_sequence:
            return "Cannot save skill: name and tool_sequence required"

        # Normalize name
        name = re.sub(r'[^a-z0-9_]', '_', name.lower().strip())
        if len(name) < 3:
            return "Skill name too short (min 3 chars)"

        # Deduplicate redundant steps before storage
        original_len = len(tool_sequence)
        tool_sequence = self._deduplicate_steps(tool_sequence)
        if len(tool_sequence) < original_len:
            logger.info(
                f"Skill '{name}': deduped {original_len} → {len(tool_sequence)} steps"
            )

        # Auto-infer category from tool names if not provided
        if not category:
            category = self._infer_category(tool_sequence)

        # Auto-generate activation triggers from goal if not provided
        if not activation_triggers:
            activation_triggers = self._extract_triggers(goal)

        # Build token index for retrieval
        text = f"{description} {goal} {' '.join(tags or [])}"
        tokens = _tokenize(text)
        tokens_str = " ".join(tokens)

        now = time.time()
        with _db_lock:
            c = self._conn.cursor()
            try:
                c.execute("""
                    INSERT INTO skills
                    (name, description, goal, tool_sequence, tags, tokens,
                     success_count, avg_duration, parent_skills, created_at, updated_at,
                     activation_triggers, required_credentials, category,
                     expected_outputs, version, cooldown_seconds, depends_on)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?,
                            ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        description = excluded.description,
                        tool_sequence = excluded.tool_sequence,
                        tags = excluded.tags,
                        tokens = excluded.tokens,
                        success_count = success_count + 1,
                        avg_duration = (avg_duration * success_count + excluded.avg_duration)
                                       / (success_count + 1),
                        updated_at = excluded.updated_at,
                        activation_triggers = excluded.activation_triggers,
                        required_credentials = excluded.required_credentials,
                        category = excluded.category,
                        expected_outputs = excluded.expected_outputs,
                        cooldown_seconds = excluded.cooldown_seconds,
                        depends_on = excluded.depends_on
                """, (
                    name, description, goal,
                    json.dumps(tool_sequence),
                    json.dumps(tags or []),
                    tokens_str,
                    duration,
                    json.dumps(parent_skills or []),
                    now, now,
                    json.dumps(activation_triggers or []),
                    json.dumps(required_credentials or []),
                    category or "general",
                    json.dumps(expected_outputs or []),
                    cooldown_seconds or 0,
                    json.dumps(depends_on or []),
                ))
                self._conn.commit()
                # Index in vector store for semantic search
                if self._vectors is not None:
                    try:
                        self._vectors.add(
                            name,
                            f"{goal} {description}",
                            {"category": category or "general",
                             "tags": tags or []},
                        )
                    except Exception as ve:
                        logger.debug(f"VectorStore add failed for '{name}': {ve}")
                logger.info(f"Skill saved: {name} ({len(tool_sequence)} steps, "
                            f"category={category})")
                return f"Skill '{name}' saved ({len(tool_sequence)} steps)"
            except Exception as e:
                logger.error(f"Failed to save skill '{name}': {e}")
                return f"Failed to save skill: {e}"

    def find_skill(self, goal, min_similarity=0.3, limit=3):
        """Find the best matching skill for a goal.

        Uses a two-phase approach:
          1. Trigger-based matching (fast, keyword/regex activation)
          2. TF-IDF similarity fallback (semantic matching)

        Args:
            goal: The user's request/goal
            min_similarity: Minimum similarity threshold (0.0-1.0)
            limit: Max number of results

        Returns:
            List of matching skills, each with:
                name, description, tool_sequence, similarity, success_count
            Sorted by similarity descending. Empty list if no match.
        """
        # Phase 1: Try exact trigger matching (Composio pattern)
        trigger_matches = self._match_triggers(goal)
        if trigger_matches:
            # Boost trigger matches by 0.3 similarity
            for m in trigger_matches:
                m["similarity"] = min(1.0, m.get("similarity", 0.7) + 0.3)
            return trigger_matches[:limit]

        # Phase 2: Try vector search (FAISS + sentence-transformers)
        if self._vectors is not None:
            try:
                vec_results = self._vectors.search(
                    goal, top_k=limit * 2, min_similarity=min_similarity
                )
                if vec_results:
                    # Enrich vector results with full skill data from DB
                    enriched = []
                    for vr in vec_results:
                        skill_name = vr["id"]
                        with _db_lock:
                            c = self._conn.cursor()
                            c.execute("""
                                SELECT name, description, goal, tool_sequence, tags,
                                       success_count, fail_count, avg_duration,
                                       parent_skills, category, version
                                FROM skills WHERE name = ?
                            """, (skill_name,))
                            row = c.fetchone()
                        if not row:
                            continue
                        # Skip skills with poor success rate
                        if row["success_count"] <= row["fail_count"]:
                            continue
                        sim = vr["similarity"]
                        # Boost for high success count
                        if row["success_count"] > 5:
                            sim *= 1.1
                        # Penalty for failures
                        total = row["success_count"] + row["fail_count"]
                        if total > 0:
                            success_rate = row["success_count"] / total
                            sim *= (0.5 + 0.5 * success_rate)
                        if sim >= min_similarity:
                            enriched.append({
                                "name": row["name"],
                                "description": row["description"],
                                "goal": row["goal"],
                                "tool_sequence": json.loads(row["tool_sequence"]),
                                "tags": json.loads(row["tags"]) if row["tags"] else [],
                                "similarity": round(sim, 3),
                                "success_count": row["success_count"],
                                "fail_count": row["fail_count"],
                                "avg_duration": row["avg_duration"],
                                "parent_skills": json.loads(row["parent_skills"]) if row["parent_skills"] else [],
                                "category": row["category"] or "general",
                                "version": row["version"] or 1,
                            })
                    if enriched:
                        enriched.sort(key=lambda x: x["similarity"], reverse=True)
                        return enriched[:limit]
            except Exception as e:
                logger.debug(f"Vector search failed, falling back to TF-IDF: {e}")

        # Phase 3: Fall back to TF-IDF similarity
        query_tokens = _tokenize(goal)
        if not query_tokens:
            return []

        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT name, description, goal, tool_sequence, tags, tokens,
                       success_count, fail_count, avg_duration, parent_skills,
                       category, version
                FROM skills
                WHERE success_count > fail_count
                ORDER BY success_count DESC
            """)
            rows = c.fetchall()

        if not rows:
            return []

        results = []
        for row in rows:
            doc_tokens = row["tokens"].split() if row["tokens"] else []
            sim = _tfidf_similarity(query_tokens, doc_tokens)

            # Boost for high success count
            if row["success_count"] > 5:
                sim *= 1.1
            # Penalty for failures
            total = row["success_count"] + row["fail_count"]
            if total > 0:
                success_rate = row["success_count"] / total
                sim *= (0.5 + 0.5 * success_rate)

            if sim >= min_similarity:
                results.append({
                    "name": row["name"],
                    "description": row["description"],
                    "goal": row["goal"],
                    "tool_sequence": json.loads(row["tool_sequence"]),
                    "tags": json.loads(row["tags"]) if row["tags"] else [],
                    "similarity": round(sim, 3),
                    "success_count": row["success_count"],
                    "fail_count": row["fail_count"],
                    "avg_duration": row["avg_duration"],
                    "parent_skills": json.loads(row["parent_skills"]) if row["parent_skills"] else [],
                    "category": row["category"] or "general",
                    "version": row["version"] or 1,
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def _match_triggers(self, text):
        """Match text against skill activation_triggers.

        Returns list of matching skills sorted by trigger hit ratio.
        """
        text_lower = text.lower()
        matches = []

        with _db_lock:
            rows = self._conn.execute(
                "SELECT name, description, goal, tool_sequence, tags, "
                "activation_triggers, category, required_credentials, "
                "cooldown_seconds, last_used_at, success_count, fail_count, version "
                "FROM skills WHERE activation_triggers != '[]' "
                "AND success_count > fail_count"
            ).fetchall()

        for row in rows:
            triggers = json.loads(row["activation_triggers"] or "[]")
            if not triggers:
                continue

            # Check if any trigger matches (regex or keyword)
            score = 0
            for trigger in triggers:
                try:
                    if re.search(trigger, text_lower):
                        score += 1
                except re.error:
                    # Plain keyword match if regex is invalid
                    if trigger.lower() in text_lower:
                        score += 1

            if score == 0:
                continue

            # Check cooldown
            cooldown = row["cooldown_seconds"] or 0
            if cooldown > 0 and row["last_used_at"]:
                elapsed = time.time() - row["last_used_at"]
                if elapsed < cooldown:
                    continue  # Still in cooldown

            matches.append({
                "name": row["name"],
                "description": row["description"],
                "goal": row["goal"],
                "tool_sequence": json.loads(row["tool_sequence"]),
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "similarity": min(1.0, score / max(len(triggers), 1)),
                "success_count": row["success_count"],
                "fail_count": row["fail_count"],
                "category": row["category"] or "general",
                "version": row["version"] or 1,
            })

        # Sort by score descending, then by version descending
        matches.sort(
            key=lambda m: (m["similarity"], m.get("version", 1)),
            reverse=True,
        )
        return matches

    def find_by_category(self, category, limit=10):
        """Find skills by category.

        Args:
            category: Category string (web, system, communication, automation, general)
            limit: Max results

        Returns:
            List of skill dicts with name, goal, tool_sequence, category.
        """
        with _db_lock:
            rows = self._conn.execute(
                "SELECT name, goal, tool_sequence, tags, category, version "
                "FROM skills WHERE category = ? ORDER BY success_count DESC LIMIT ?",
                (category, limit)
            ).fetchall()
        return [{"name": r["name"], "goal": r["goal"],
                 "tool_sequence": json.loads(r["tool_sequence"]),
                 "category": r["category"] or "general"} for r in rows]

    def check_credentials(self, skill_name):
        """Check if required credentials are available for a skill.

        Args:
            skill_name: Name of the skill to check

        Returns:
            Tuple of (ok: bool, missing_list: list of str)
        """
        with _db_lock:
            row = self._conn.execute(
                "SELECT required_credentials FROM skills WHERE name = ?",
                (skill_name,)
            ).fetchone()
        if not row:
            return True, []

        creds = json.loads(row["required_credentials"] or "[]")
        if not creds:
            return True, []

        # Check against config
        try:
            from config import load_config
            cfg = load_config()
        except Exception:
            return False, creds

        missing = []
        for cred in creds:
            if cred not in cfg or not cfg[cred]:
                missing.append(cred)
        return len(missing) == 0, missing

    def bump_version(self, skill_name):
        """Increment version when a skill is refined.

        Args:
            skill_name: Name of the skill to bump
        """
        with _db_lock:
            self._conn.execute(
                "UPDATE skills SET version = version + 1 WHERE name = ?",
                (skill_name,)
            )
            self._conn.commit()

    def remove_skill(self, skill_name):
        """Remove a skill by name.

        Args:
            skill_name: Name of the skill to remove
        """
        with _db_lock:
            self._conn.execute("DELETE FROM skills WHERE name = ?", (skill_name,))
            # Also clean up reflections
            self._conn.execute(
                "DELETE FROM skill_reflections WHERE skill_name = ?", (skill_name,)
            )
            self._conn.commit()

    def get_skill(self, name):
        """Get a specific skill by name."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("SELECT * FROM skills WHERE name = ?", (name,))
            row = c.fetchone()

        if not row:
            return None

        return {
            "name": row["name"],
            "description": row["description"],
            "goal": row["goal"],
            "tool_sequence": json.loads(row["tool_sequence"]),
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "success_count": row["success_count"],
            "fail_count": row["fail_count"],
        }

    def record_use(self, name, success, duration=0.0):
        """Record a skill execution result (success or failure)."""
        with _db_lock:
            c = self._conn.cursor()
            if success:
                c.execute("""
                    UPDATE skills SET
                        success_count = success_count + 1,
                        avg_duration = (avg_duration * success_count + ?) / (success_count + 1),
                        last_used_at = ?,
                        updated_at = ?
                    WHERE name = ?
                """, (duration, time.time(), time.time(), name))
            else:
                c.execute("""
                    UPDATE skills SET
                        fail_count = fail_count + 1,
                        updated_at = ?
                    WHERE name = ?
                """, (time.time(), name))
            self._conn.commit()

    def add_reflection(self, skill_name, reflection):
        """Add a reflection note to a skill (for Reflexion pattern).

        Reflections are lessons learned from failed executions that should
        inform future attempts.
        """
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT INTO skill_reflections (skill_name, reflection, created_at)
                VALUES (?, ?, ?)
            """, (skill_name, reflection, time.time()))
            # Keep max 5 reflections per skill
            c.execute("""
                DELETE FROM skill_reflections
                WHERE skill_name = ? AND id NOT IN (
                    SELECT id FROM skill_reflections
                    WHERE skill_name = ?
                    ORDER BY created_at DESC LIMIT 5
                )
            """, (skill_name, skill_name))
            self._conn.commit()

    def get_reflections(self, skill_name, limit=3):
        """Get reflections for a skill (most recent first)."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT reflection, created_at FROM skill_reflections
                WHERE skill_name = ?
                ORDER BY created_at DESC LIMIT ?
            """, (skill_name, limit))
            return [{"reflection": row["reflection"], "time": row["created_at"]}
                    for row in c.fetchall()]

    def refine_skill(self, name, new_tool_sequence, improvement_note=""):
        """Refine a skill with an improved tool sequence.

        Only updates if the new sequence is different from the existing one.
        """
        skill = self.get_skill(name)
        if not skill:
            return f"Skill '{name}' not found"

        old_seq = json.dumps(skill["tool_sequence"])
        new_seq = json.dumps(new_tool_sequence)
        if old_seq == new_seq:
            return f"Skill '{name}' already up to date"

        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                UPDATE skills SET
                    tool_sequence = ?,
                    updated_at = ?
                WHERE name = ?
            """, (new_seq, time.time(), name))
            self._conn.commit()

        if improvement_note:
            self.add_reflection(name, f"Refined: {improvement_note}")

        logger.info(f"Skill refined: {name}")
        return f"Skill '{name}' refined"

    def list_skills(self, limit=20):
        """List all skills sorted by usage."""
        with _db_lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT name, description, success_count, fail_count, tags,
                       created_at, updated_at
                FROM skills
                ORDER BY success_count DESC, updated_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in c.fetchall()]

    def generate_skill_name(self, goal, llm_fn=None):
        """Generate a skill name from a goal description.

        Uses LLM if available, otherwise heuristic.
        """
        if llm_fn:
            try:
                resp = llm_fn(
                    f"Generate a short snake_case function name (3-6 words) for this task: '{goal}'\n"
                    f"Reply with ONLY the function name, nothing else. Example: order_pizza_online"
                )
                if resp:
                    name = re.sub(r'[^a-z0-9_]', '_', resp.strip().lower())
                    name = re.sub(r'_+', '_', name).strip('_')
                    if 3 <= len(name) <= 40:
                        return name
            except Exception:
                pass

        # Heuristic: extract key words from goal
        words = _tokenize(goal)
        # Take first 4 meaningful words
        name_words = words[:4] if words else ["unnamed_skill"]
        return "_".join(name_words)[:40]

    def cleanup(self, max_age_days=90, min_success=0):
        """Remove old, unused, or consistently failing skills."""
        cutoff = time.time() - (max_age_days * 86400)
        total_deleted = 0
        # Collect names of skills that will be deleted (for vector store cleanup)
        _to_remove_names = []
        with _db_lock:
            c = self._conn.cursor()
            # Collect names before deletion for vector store cleanup
            c.execute("""
                SELECT name FROM skills
                WHERE fail_count > success_count * 2 AND fail_count >= 3
            """)
            _to_remove_names.extend(row["name"] for row in c.fetchall())
            # Remove skills that consistently fail
            c.execute("""
                DELETE FROM skills
                WHERE fail_count > success_count * 2 AND fail_count >= 3
            """)
            total_deleted += c.rowcount

            c.execute("""
                SELECT name FROM skills
                WHERE last_used_at IS NOT NULL AND last_used_at < ?
                  AND success_count <= ?
            """, (cutoff, min_success))
            _to_remove_names.extend(row["name"] for row in c.fetchall())
            # Remove very old unused skills
            c.execute("""
                DELETE FROM skills
                WHERE last_used_at IS NOT NULL AND last_used_at < ?
                  AND success_count <= ?
            """, (cutoff, min_success))
            total_deleted += c.rowcount

            stale_cutoff = time.time() - (7 * 86400)
            c.execute("""
                SELECT name FROM skills
                WHERE success_count = 0 AND created_at < ?
            """, (stale_cutoff,))
            _to_remove_names.extend(row["name"] for row in c.fetchall())
            # Remove never-successful skills older than 7 days
            c.execute("""
                DELETE FROM skills
                WHERE success_count = 0 AND created_at < ?
            """, (stale_cutoff,))
            total_deleted += c.rowcount
            # Also clean up orphaned reflections for deleted skills
            c.execute("""
                DELETE FROM skill_reflections
                WHERE skill_name NOT IN (SELECT name FROM skills)
            """)
            self._conn.commit()
            if total_deleted:
                logger.info(f"Skill cleanup: removed {total_deleted} stale skills")

        # Remove cleaned-up skills from vector store
        if self._vectors is not None and _to_remove_names:
            for name in _to_remove_names:
                try:
                    self._vectors.remove(name)
                except Exception as ve:
                    logger.debug(f"VectorStore remove failed for '{name}': {ve}")
