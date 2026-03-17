"""
Episodic Memory — searchable SQLite archive of all interactions, skills, and failures.

Tables: episodes, skills, failures, routines, user_facts
Full-text search via FTS5 (falls back to LIKE if unavailable).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from core.paths import MEMORY_DB
    DB_PATH = MEMORY_DB
except ImportError:
    DB_PATH = os.path.join("data", "episodic_memory.db")


@dataclass
class Episode:
    id: int = 0
    timestamp: float = 0.0
    user_input: str = ""
    response: str = ""
    tools_used: List[str] = field(default_factory=list)
    success: bool = True
    topic: str = ""
    emotion: str = "neutral"
    duration_ms: int = 0


@dataclass
class Skill:
    id: int = 0
    goal: str = ""
    tool_sequence: List[Dict] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    created_at: float = 0.0
    last_used: float = 0.0

    @property
    def reliability(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.5


class EpisodicMemory:
    """Persistent memory backed by SQLite + FTS5."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    user_input TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',
                    tools_used TEXT DEFAULT '[]',
                    success INTEGER DEFAULT 1,
                    topic TEXT DEFAULT '',
                    emotion TEXT DEFAULT 'neutral',
                    duration_ms INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    goal_normalized TEXT NOT NULL DEFAULT '',
                    tool_sequence TEXT NOT NULL DEFAULT '[]',
                    success_count INTEGER DEFAULT 1,
                    fail_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_used REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    goal TEXT NOT NULL,
                    error TEXT NOT NULL,
                    context TEXT DEFAULT '',
                    lesson TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_facts (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    source TEXT DEFAULT 'inferred',
                    confidence REAL DEFAULT 0.5,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);
                CREATE INDEX IF NOT EXISTS idx_episodes_topic ON episodes(topic);
                CREATE INDEX IF NOT EXISTS idx_skills_goal ON skills(goal_normalized);
            """)

            # FTS5 for full-text search
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                        user_input, response, topic,
                        content='episodes', content_rowid='id'
                    )
                """)
            except sqlite3.OperationalError:
                pass  # FTS5 not available

            conn.commit()
        finally:
            conn.close()

    # ── Episodes ──────────────────────────────────────────────────────────────

    def log_episode(self, user_input: str, response: str = "",
                    tools: Optional[List[str]] = None, success: bool = True,
                    topic: str = "", emotion: str = "neutral",
                    duration_ms: int = 0) -> int:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO episodes (timestamp, user_input, response, tools_used, "
                "success, topic, emotion, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), user_input, response, json.dumps(tools or []),
                 int(success), topic, emotion, duration_ms),
            )
            ep_id = cur.lastrowid
            try:
                conn.execute(
                    "INSERT INTO episodes_fts (rowid, user_input, response, topic) "
                    "VALUES (?,?,?,?)",
                    (ep_id, user_input, response, topic),
                )
            except Exception:
                pass
            conn.commit()
            return ep_id
        finally:
            conn.close()

    def search(self, query: str, limit: int = 10) -> List[Episode]:
        conn = self._get_conn()
        try:
            try:
                rows = conn.execute(
                    "SELECT e.* FROM episodes e JOIN episodes_fts f ON e.id = f.rowid "
                    "WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            except Exception:
                rows = conn.execute(
                    "SELECT * FROM episodes WHERE user_input LIKE ? OR response LIKE ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", limit),
                ).fetchall()
            return [self._row_to_episode(r) for r in rows]
        finally:
            conn.close()

    def get_recent(self, limit: int = 20) -> List[Episode]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_episode(r) for r in rows]
        finally:
            conn.close()

    # ── Skills ────────────────────────────────────────────────────────────────

    def learn_skill(self, goal: str, tool_sequence: List[Dict]) -> int:
        normalized = goal.lower().strip()
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM skills WHERE goal_normalized = ?", (normalized,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE skills SET success_count = success_count + 1, last_used = ? "
                    "WHERE id = ?",
                    (time.time(), existing["id"]),
                )
                conn.commit()
                return existing["id"]
            cur = conn.execute(
                "INSERT INTO skills (goal, goal_normalized, tool_sequence, created_at, last_used) "
                "VALUES (?,?,?,?,?)",
                (goal, normalized, json.dumps(tool_sequence), time.time(), time.time()),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def find_skill(self, goal: str, min_reliability: float = 0.7) -> Optional[Skill]:
        normalized = goal.lower().strip()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM skills WHERE goal_normalized = ? AND "
                "(success_count * 1.0 / MAX(success_count + fail_count, 1)) >= ?",
                (normalized, min_reliability),
            ).fetchone()
            if row:
                return self._row_to_skill(row)

            rows = conn.execute(
                "SELECT * FROM skills WHERE goal_normalized LIKE ? AND "
                "(success_count * 1.0 / MAX(success_count + fail_count, 1)) >= ? "
                "ORDER BY success_count DESC LIMIT 5",
                (f"%{normalized}%", min_reliability),
            ).fetchall()
            return self._row_to_skill(rows[0]) if rows else None
        finally:
            conn.close()

    def mark_skill_success(self, skill_id: int) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE skills SET success_count = success_count + 1, last_used = ? "
                "WHERE id = ?",
                (time.time(), skill_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_skill_failure(self, skill_id: int) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE skills SET fail_count = fail_count + 1 WHERE id = ?",
                (skill_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # ── Failures ─────────────────────────────────────────────────────────────

    def log_failure(self, goal: str, error: str,
                    context: str = "", lesson: str = "") -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO failures (timestamp, goal, error, context, lesson) "
                "VALUES (?,?,?,?,?)",
                (time.time(), goal, error, context, lesson),
            )
            conn.commit()
        finally:
            conn.close()

    def get_failures_for(self, goal: str, limit: int = 5) -> List[Dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM failures WHERE goal LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f"%{goal}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── User facts ────────────────────────────────────────────────────────────

    def set_user_fact(self, key: str, value: str,
                      source: str = "inferred", confidence: float = 0.5) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_facts (key, value, source, confidence, updated_at) "
                "VALUES (?,?,?,?,?)",
                (key, value, source, confidence, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_user_fact(self, key: str) -> Optional[str]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM user_facts WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def get_all_user_facts(self) -> Dict[str, str]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT key, value FROM user_facts").fetchall()
            return {r["key"]: r["value"] for r in rows}
        finally:
            conn.close()

    def get_stats(self) -> Dict:
        conn = self._get_conn()
        try:
            return {
                "episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                "skills":   conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
                "failures": conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0],
                "user_facts": conn.execute("SELECT COUNT(*) FROM user_facts").fetchone()[0],
            }
        finally:
            conn.close()

    def close(self) -> None:
        pass  # Per-call connections; nothing to close

    # ── Converters ────────────────────────────────────────────────────────────

    def _row_to_episode(self, row) -> Episode:
        return Episode(
            id=row["id"], timestamp=row["timestamp"],
            user_input=row["user_input"], response=row["response"],
            tools_used=json.loads(row["tools_used"]),
            success=bool(row["success"]), topic=row["topic"],
            emotion=row["emotion"], duration_ms=row["duration_ms"],
        )

    def _row_to_skill(self, row) -> Skill:
        return Skill(
            id=row["id"], goal=row["goal"],
            tool_sequence=json.loads(row["tool_sequence"]),
            success_count=row["success_count"], fail_count=row["fail_count"],
            created_at=row["created_at"], last_used=row["last_used"],
        )


# Module-level singleton
episodic = EpisodicMemory()
