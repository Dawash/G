"""
Failure Journal — structured failure corpus for debugging and learning.

Every failed task is recorded with:
  - user_goal: what the user asked
  - chosen_route: which strategy/tier was selected
  - tool_sequence: ordered list of tool calls attempted
  - error_class: categorized failure type
  - model_prompt: the LLM prompt that led to the failure (if applicable)
  - model_response: what the LLM returned
  - diagnosis: what went wrong
  - human_intervened: bool
  - timestamp

Failure patterns are clustered:
  - wrong_window: action on wrong app
  - stale_selector: UI element not found
  - modal_popup: unexpected dialog blocked action
  - timing_race: action too fast/slow
  - hallucinated_args: LLM gave bad tool arguments
  - permission_boundary: blocked by OS/security
  - app_layout_drift: UI changed from expected
  - network_failure: web/API request failed
  - tool_not_found: referenced a tool that doesn't exist
  - empty_response: LLM returned nothing useful
  - timeout: operation took too long
  - unknown: unclassifiable failure

Storage: SQLite table `failure_journal` in the existing memory.db.
Thread-safe via Lock for concurrent access from brain/agent threads.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Error classification
# ===================================================================

ERROR_CLASSES = frozenset({
    "wrong_window",
    "stale_selector",
    "modal_popup",
    "timing_race",
    "hallucinated_args",
    "permission_boundary",
    "app_layout_drift",
    "network_failure",
    "tool_not_found",
    "empty_response",
    "timeout",
    "unknown",
})


def classify_error(error_text: str, tool_name: str = "",
                   tool_result: str = "") -> str:
    """Auto-classify an error into one of the known ERROR_CLASSES.

    Uses keyword matching on the error text and tool result.
    Falls back to 'unknown' if no pattern matches.
    """
    text = f"{error_text} {tool_result}".lower()

    if any(kw in text for kw in ("not found", "no such element", "selector",
                                   "element not found", "stale")):
        return "stale_selector"

    if any(kw in text for kw in ("wrong window", "wrong app", "focused on",
                                   "active window mismatch")):
        return "wrong_window"

    if any(kw in text for kw in ("popup", "dialog", "overlay", "modal",
                                   "sign in", "login wall", "subscription")):
        return "modal_popup"

    if any(kw in text for kw in ("timeout", "timed out", "too slow",
                                   "took too long", "deadline")):
        return "timeout"

    if any(kw in text for kw in ("too fast", "not ready", "race",
                                   "not loaded", "loading")):
        return "timing_race"

    if any(kw in text for kw in ("permission", "denied", "access denied",
                                   "blocked", "uac", "admin required",
                                   "elevation", "unauthorized")):
        return "permission_boundary"

    if any(kw in text for kw in ("hallucinated", "invalid argument",
                                   "bad arg", "unexpected argument",
                                   "missing required")):
        return "hallucinated_args"

    if any(kw in text for kw in ("layout", "ui changed", "different position",
                                   "moved", "redesigned")):
        return "app_layout_drift"

    if any(kw in text for kw in ("network", "connection", "dns", "http",
                                   "ssl", "fetch failed", "unreachable")):
        return "network_failure"

    if any(kw in text for kw in ("tool not found", "unknown tool",
                                   "no handler", "not registered")):
        return "tool_not_found"

    if any(kw in text for kw in ("empty", "no response", "none",
                                   "returned nothing")):
        return "empty_response"

    return "unknown"


# ===================================================================
# Failure record
# ===================================================================

@dataclass
class FailureRecord:
    """A single recorded failure."""
    id: Optional[int] = None
    user_goal: str = ""
    chosen_route: str = ""
    tool_sequence: list = field(default_factory=list)
    error_class: str = "unknown"
    error_text: str = ""
    model_prompt: str = ""
    model_response: str = ""
    diagnosis: str = ""
    human_intervened: bool = False
    timestamp: float = field(default_factory=time.time)

    @property
    def timestamp_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")


# ===================================================================
# Failure journal (SQLite-backed, thread-safe)
# ===================================================================

# Default DB path — same as memory.db
_DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "memory.db")


class FailureJournal:
    """Persistent failure log with query and analytics capabilities.

    Stores failure records in SQLite (failure_journal table in memory.db).
    Thread-safe — all DB access goes through a Lock.
    """

    def __init__(self, db_path: str = _DB_FILE):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_table()

    def _init_table(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS failure_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_goal TEXT NOT NULL,
                    chosen_route TEXT DEFAULT '',
                    tool_sequence TEXT DEFAULT '[]',
                    error_class TEXT DEFAULT 'unknown',
                    error_text TEXT DEFAULT '',
                    model_prompt TEXT DEFAULT '',
                    model_response TEXT DEFAULT '',
                    diagnosis TEXT DEFAULT '',
                    human_intervened INTEGER DEFAULT 0,
                    timestamp REAL NOT NULL
                )
            """)
            # Index on error_class for fast filtering
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_fj_error_class
                ON failure_journal(error_class)
            """)
            # Index on timestamp for recent queries
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_fj_timestamp
                ON failure_journal(timestamp)
            """)
            self._conn.commit()

    def close(self):
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------

    def record_failure(self, goal: str, route: str = "",
                       tool_sequence: Optional[list] = None,
                       error_class: str = "unknown",
                       error_text: str = "",
                       model_prompt: str = "",
                       model_response: str = "",
                       diagnosis: str = "",
                       human_intervened: bool = False) -> int:
        """Record a failure. Returns the row ID of the inserted record.

        Args:
            goal: What the user asked for.
            route: Which strategy/tier was selected (e.g. "quick", "agent", "CLI").
            tool_sequence: Ordered list of tool calls attempted.
                           Each entry can be a string or dict.
            error_class: One of ERROR_CLASSES.
            error_text: Raw error message or traceback snippet.
            model_prompt: The LLM system/user prompt (truncated for storage).
            model_response: What the LLM returned (truncated).
            diagnosis: Human or LLM diagnosis of what went wrong.
            human_intervened: Whether the user had to step in.

        Returns:
            The row ID of the new failure record.
        """
        tool_seq = tool_sequence or []
        # Validate error class
        if error_class not in ERROR_CLASSES:
            logger.warning(f"Unknown error_class '{error_class}', using 'unknown'")
            error_class = "unknown"

        # Truncate large text fields to keep DB manageable
        model_prompt = (model_prompt or "")[:2000]
        model_response = (model_response or "")[:2000]
        error_text = (error_text or "")[:1000]
        diagnosis = (diagnosis or "")[:1000]

        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO failure_journal
                   (user_goal, chosen_route, tool_sequence, error_class,
                    error_text, model_prompt, model_response, diagnosis,
                    human_intervened, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (goal, route, json.dumps(tool_seq), error_class,
                 error_text, model_prompt, model_response, diagnosis,
                 1 if human_intervened else 0, now),
            )
            self._conn.commit()
            row_id = cursor.lastrowid
            logger.info(f"Failure recorded #{row_id}: [{error_class}] {goal[:60]}")
            return row_id

    # -----------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------

    def get_failures(self, limit: int = 50,
                     error_class: Optional[str] = None,
                     since: Optional[float] = None) -> list[FailureRecord]:
        """Query failure records.

        Args:
            limit: Max records to return.
            error_class: Filter by error class (optional).
            since: Only records after this timestamp (optional).

        Returns:
            List of FailureRecord, newest first.
        """
        clauses = []
        params = []
        if error_class:
            clauses.append("error_class = ?")
            params.append(error_class)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM failure_journal {where} "
                f"ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()

        return [self._row_to_record(r) for r in rows]

    def get_failure_by_id(self, record_id: int) -> Optional[FailureRecord]:
        """Get a single failure record by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM failure_journal WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row:
            return self._row_to_record(row)
        return None

    def get_failure_stats(self) -> dict:
        """Aggregate failure statistics.

        Returns dict with:
          - total: total failure count
          - by_error_class: {class: count}
          - by_route: {route: count}
          - recent_24h: count in last 24 hours
          - top_goals: most common failure goals (top 10)
          - human_intervention_rate: fraction of failures where human stepped in
        """
        with self._lock:
            # Total
            total = self._conn.execute(
                "SELECT COUNT(*) FROM failure_journal"
            ).fetchone()[0]

            if total == 0:
                return {
                    "total": 0,
                    "by_error_class": {},
                    "by_route": {},
                    "recent_24h": 0,
                    "top_goals": [],
                    "human_intervention_rate": 0.0,
                }

            # By error class
            rows = self._conn.execute(
                "SELECT error_class, COUNT(*) as cnt "
                "FROM failure_journal GROUP BY error_class "
                "ORDER BY cnt DESC"
            ).fetchall()
            by_class = {r[0]: r[1] for r in rows}

            # By route
            rows = self._conn.execute(
                "SELECT chosen_route, COUNT(*) as cnt "
                "FROM failure_journal GROUP BY chosen_route "
                "ORDER BY cnt DESC"
            ).fetchall()
            by_route = {r[0]: r[1] for r in rows}

            # Recent 24h
            cutoff = time.time() - 86400
            recent = self._conn.execute(
                "SELECT COUNT(*) FROM failure_journal WHERE timestamp >= ?",
                (cutoff,),
            ).fetchone()[0]

            # Top goals
            rows = self._conn.execute(
                "SELECT user_goal, COUNT(*) as cnt "
                "FROM failure_journal GROUP BY user_goal "
                "ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
            top_goals = [(r[0], r[1]) for r in rows]

            # Human intervention rate
            human_count = self._conn.execute(
                "SELECT COUNT(*) FROM failure_journal WHERE human_intervened = 1"
            ).fetchone()[0]

        return {
            "total": total,
            "by_error_class": by_class,
            "by_route": by_route,
            "recent_24h": recent,
            "top_goals": top_goals,
            "human_intervention_rate": human_count / total if total else 0.0,
        }

    def get_similar_failures(self, goal: str, limit: int = 3) -> list[FailureRecord]:
        """Find past failures with similar user goals.

        Uses SequenceMatcher for lightweight fuzzy matching (no external deps).
        Returns up to `limit` records sorted by similarity (highest first).
        """
        if not goal or not goal.strip():
            return []

        goal_lower = goal.lower().strip()

        # Fetch recent failures to compare against (last 200)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM failure_journal ORDER BY timestamp DESC LIMIT 200"
            ).fetchall()

        if not rows:
            return []

        # Score each record by goal similarity
        scored = []
        for row in rows:
            record = self._row_to_record(row)
            similarity = SequenceMatcher(
                None, goal_lower, record.user_goal.lower()
            ).ratio()
            if similarity >= 0.4:  # Minimum threshold
                scored.append((similarity, record))

        # Sort by similarity descending, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    def count_by_tool(self) -> dict[str, int]:
        """Count failures involving each tool (across all tool_sequence entries)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool_sequence FROM failure_journal"
            ).fetchall()

        tool_counts: dict[str, int] = {}
        for row in rows:
            try:
                seq = json.loads(row[0]) if row[0] else []
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in seq:
                # entry can be a string "tool_name" or dict {"tool": "...", ...}
                if isinstance(entry, str):
                    name = entry
                elif isinstance(entry, dict):
                    name = entry.get("tool", entry.get("name", ""))
                else:
                    continue
                if name:
                    tool_counts[name] = tool_counts.get(name, 0) + 1

        return dict(sorted(tool_counts.items(), key=lambda x: x[1], reverse=True))

    def prune_old(self, max_age_days: int = 30) -> int:
        """Delete failure records older than max_age_days.

        Returns the number of records deleted.
        """
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM failure_journal WHERE timestamp < ?",
                (cutoff,),
            )
            self._conn.commit()
            deleted = cursor.rowcount
        if deleted:
            logger.info(f"Pruned {deleted} failure records older than {max_age_days} days")
        return deleted

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _row_to_record(self, row) -> FailureRecord:
        """Convert a sqlite3.Row to a FailureRecord."""
        try:
            tool_seq = json.loads(row["tool_sequence"]) if row["tool_sequence"] else []
        except (json.JSONDecodeError, TypeError):
            tool_seq = []

        return FailureRecord(
            id=row["id"],
            user_goal=row["user_goal"],
            chosen_route=row["chosen_route"] or "",
            tool_sequence=tool_seq,
            error_class=row["error_class"] or "unknown",
            error_text=row["error_text"] or "",
            model_prompt=row["model_prompt"] or "",
            model_response=row["model_response"] or "",
            diagnosis=row["diagnosis"] or "",
            human_intervened=bool(row["human_intervened"]),
            timestamp=row["timestamp"],
        )


# ===================================================================
# Module-level singleton
# ===================================================================

_default_journal: Optional[FailureJournal] = None


def get_default_journal() -> FailureJournal:
    """Get or create the default failure journal (uses memory.db)."""
    global _default_journal
    if _default_journal is None:
        _default_journal = FailureJournal()
    return _default_journal


def record_failure(**kwargs) -> int:
    """Convenience function: record a failure to the default journal."""
    return get_default_journal().record_failure(**kwargs)
