"""
Persistent memory system — remembers across sessions.

Three layers:
  1. Session memory   — what happened this session (apps opened, topics)
  2. Long-term memory — user preferences, facts, learned habits
  3. Habit tracker    — detects usage patterns over time

All backed by SQLite for fast, reliable local storage.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")


class MemoryStore:
    """SQLite-backed persistent memory with search."""

    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Enable WAL mode for safe concurrent reads/writes across threads
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._private_mode = False
        self._init_db()

    def _init_db(self):
        c = self._conn.cursor()
        # Long-term facts and preferences
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                UNIQUE(category, key)
            )
        """)
        # Session events log
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT,
                timestamp REAL NOT NULL
            )
        """)
        # Usage patterns for habit tracking
        c.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity TEXT,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                timestamp REAL NOT NULL
            )
        """)
        self._conn.commit()

    # --- Long-term memory ---

    def remember(self, category, key, value):
        """Store or update a memory."""
        now = time.time()
        with self._lock:
            c = self._conn.cursor()
            c.execute("""
                INSERT INTO memories (category, key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    access_count = access_count + 1
            """, (category, key, value, now, now))
            self._conn.commit()

    def recall(self, category, key):
        """Retrieve a specific memory."""
        with self._lock:
            c = self._conn.cursor()
            c.execute("""
                UPDATE memories SET access_count = access_count + 1, updated_at = ?
                WHERE category = ? AND key = ?
            """, (time.time(), category, key))
            c.execute(
                "SELECT value FROM memories WHERE category = ? AND key = ?",
                (category, key),
            )
            row = c.fetchone()
            self._conn.commit()
            return row["value"] if row else None

    def search(self, query, limit=5):
        """Search memories by keyword in key or value."""
        with self._lock:
            c = self._conn.cursor()
            c.execute("""
                SELECT category, key, value FROM memories
                WHERE key LIKE ? OR value LIKE ?
                ORDER BY access_count DESC, updated_at DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit))
            return [dict(row) for row in c.fetchall()]

    def get_category(self, category):
        """Get all memories in a category."""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT key, value FROM memories WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            )
            return {row["key"]: row["value"] for row in c.fetchall()}

    def forget(self, category, key):
        """Remove a specific memory."""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "DELETE FROM memories WHERE category = ? AND key = ?",
                (category, key),
            )
            self._conn.commit()

    def forget_category(self, category):
        """Remove all memories in a category."""
        with self._lock:
            c = self._conn.cursor()
            c.execute("DELETE FROM memories WHERE category = ?", (category,))
            self._conn.commit()

    def get_all_facts(self):
        """Return all memories grouped by category."""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT category, key, value FROM memories ORDER BY category, updated_at DESC")
            result = {}
            for row in c.fetchall():
                cat = row["category"]
                if cat not in result:
                    result[cat] = []
                result[cat].append({"key": row["key"], "value": row["value"]})
            return result

    def count_memories(self):
        """Count total stored memories."""
        with self._lock:
            c = self._conn.cursor()
            c.execute("SELECT COUNT(*) FROM memories")
            return c.fetchone()[0]

    # --- Private mode ---

    def set_private_mode(self, enabled):
        """Toggle private mode. When enabled, events and usage are not logged."""
        self._private_mode = enabled

    @property
    def is_private(self):
        return self._private_mode

    # --- Session events ---

    def log_event(self, session_id, event_type, data=None):
        """Log a session event (app opened, search made, etc.)."""
        if self._private_mode:
            return
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "INSERT INTO session_events (session_id, event_type, data, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, event_type, json.dumps(data) if data else None, time.time()),
            )
            self._conn.commit()

    def get_session_events(self, session_id):
        """Get all events from a session."""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT event_type, data, timestamp FROM session_events WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            )
            return [
                {
                    "event_type": row["event_type"],
                    "data": json.loads(row["data"]) if row["data"] else None,
                    "time": datetime.fromtimestamp(row["timestamp"]).strftime("%H:%M"),
                }
                for row in c.fetchall()
            ]

    # --- Usage logging for habit detection ---

    def log_usage(self, action, entity=None):
        """Log a usage event for habit tracking."""
        if self._private_mode:
            return
        now = datetime.now()
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "INSERT INTO usage_log (action, entity, hour, day_of_week, timestamp) VALUES (?, ?, ?, ?, ?)",
                (action, entity, now.hour, now.weekday(), time.time()),
            )
            self._conn.commit()

    def cleanup(self, max_events_age_days=30, max_usage_age_days=90):
        """Prune old session events and usage logs to prevent unbounded growth.

        Args:
            max_events_age_days: Delete session events older than this.
            max_usage_age_days: Delete usage logs older than this.

        Returns:
            dict with counts of deleted rows.
        """
        now = time.time()
        events_cutoff = now - (max_events_age_days * 86400)
        usage_cutoff = now - (max_usage_age_days * 86400)

        with self._lock:
            c = self._conn.cursor()
            c.execute("DELETE FROM session_events WHERE timestamp < ?", (events_cutoff,))
            events_deleted = c.rowcount
            c.execute("DELETE FROM usage_log WHERE timestamp < ?", (usage_cutoff,))
            usage_deleted = c.rowcount
            self._conn.commit()

            # Reclaim disk space if we deleted a lot
            if events_deleted + usage_deleted > 1000:
                c.execute("VACUUM")

        logger.info(f"Memory cleanup: {events_deleted} events, {usage_deleted} usage rows deleted")
        return {"events_deleted": events_deleted, "usage_deleted": usage_deleted}

    def get_db_size_mb(self):
        """Get the current database file size in MB."""
        try:
            return os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return 0.0

    def close(self):
        with self._lock:
            self._conn.close()


class UserPreferences:
    """Track and learn user preferences over time."""

    # Default values for known preference keys
    DEFAULTS = {
        "response_style": "normal",       # concise / normal / detailed
        "confirmation_level": "normal",   # strict / normal / relaxed
        "speaking_style": "casual",       # formal / casual / playful
        "preferred_news": "general",      # comma-separated categories
    }

    def __init__(self, store: MemoryStore):
        self.store = store

    def set_preference(self, key, value):
        """Explicitly set a user preference."""
        self.store.remember("preferences", key, value)

    def get_preference(self, key, default=None):
        """Get a user preference."""
        val = self.store.recall("preferences", key)
        if val is not None:
            return val
        if default is not None:
            return default
        return self.DEFAULTS.get(key)

    def get_all_preferences(self):
        """Return all stored preferences merged with defaults."""
        stored = self.store.get_category("preferences")
        result = dict(self.DEFAULTS)
        result.update(stored)
        return result

    def learn_from_usage(self):
        """Analyze usage patterns to learn preferences."""
        with self.store._lock:
            c = self.store._conn.cursor()

            # Most opened apps
            c.execute("""
                SELECT entity, COUNT(*) as cnt FROM usage_log
                WHERE action = 'open_app' AND entity IS NOT NULL
                GROUP BY entity ORDER BY cnt DESC LIMIT 5
            """)
            top_apps = [row[0] for row in c.fetchall()]

            # Most common search topics
            c.execute("""
                SELECT entity, COUNT(*) as cnt FROM usage_log
                WHERE action = 'google_search' AND entity IS NOT NULL
                GROUP BY entity ORDER BY cnt DESC LIMIT 5
            """)
            top_searches = [row[0] for row in c.fetchall()]

        # remember() acquires its own lock, so call outside the lock
        if top_apps:
            self.store.remember("learned", "favorite_apps", json.dumps(top_apps))
        if top_searches:
            self.store.remember("learned", "common_searches", json.dumps(top_searches))

    def get_favorite_apps(self):
        """Get the user's most frequently opened apps."""
        raw = self.store.recall("learned", "favorite_apps")
        return json.loads(raw) if raw else []

    # --- App category defaults ---

    _APP_CATEGORIES = {
        "browser": ["chrome", "firefox", "edge", "brave", "opera", "vivaldi"],
        "web browser": ["chrome", "firefox", "edge", "brave", "opera", "vivaldi"],
        "default browser": ["chrome", "firefox", "edge", "brave", "opera", "vivaldi"],
        "default web browser": ["chrome", "firefox", "edge", "brave", "opera", "vivaldi"],
        "internet browser": ["chrome", "firefox", "edge", "brave", "opera", "vivaldi"],
        "editor": ["notepad", "notepad++", "vscode", "code", "sublime", "vim", "nano"],
        "text editor": ["notepad", "notepad++", "vscode", "code", "sublime"],
        "code editor": ["vscode", "code", "sublime", "notepad++", "vim"],
        "terminal": ["windows terminal", "cmd", "powershell", "git bash"],
        "command prompt": ["cmd", "windows terminal", "powershell"],
        "file manager": ["explorer", "files"],
        "music player": ["spotify", "vlc", "foobar", "winamp"],
        "video player": ["vlc", "mpv", "windows media player"],
        "email": ["outlook", "thunderbird", "gmail"],
        "email client": ["outlook", "thunderbird", "gmail"],
        "calculator": ["calculator", "calc"],
    }

    def resolve_app_category(self, name):
        """Resolve generic app names to user's preferred app.

        E.g. 'browser' → 'Chrome' (if user has used Chrome most).
        Returns the original name if not a category keyword.
        """
        lower = name.lower().strip()
        # Check if it's a category keyword
        if lower not in self._APP_CATEGORIES:
            # Strip filler words: "my browser", "a browser", "the default web browser"
            import re
            m = re.match(r'^(?:my |a |the |an )?(.+)$', lower)
            if m:
                lower = m.group(1).strip()
            if lower not in self._APP_CATEGORIES:
                # Try stripping "default " prefix too
                if lower.startswith("default "):
                    lower = lower[8:].strip()
                if lower not in self._APP_CATEGORIES:
                    return name

        # Check if user has a saved preference
        pref = self.store.recall("app_defaults", lower)
        if pref:
            return pref

        # Auto-detect from usage history
        candidates = self._APP_CATEGORIES[lower]
        with self.store._lock:
            cur = self.store._conn.cursor()
            placeholders = ",".join("?" for _ in candidates)
            cur.execute(f"""
                SELECT entity, COUNT(*) as cnt FROM usage_log
                WHERE action = 'open_app' AND LOWER(entity) IN ({placeholders})
                GROUP BY entity ORDER BY cnt DESC LIMIT 1
            """, candidates)
            row = cur.fetchone()
        if row:
            return row[0]

        # Fallback: first candidate
        return candidates[0]

    def set_app_default(self, category, app_name):
        """Set preferred app for a category (e.g. 'browser' → 'Firefox')."""
        self.store.remember("app_defaults", category.lower(), app_name)

    # --- Nicknames (Phase 10) ---

    def set_nickname(self, nickname, actual_name):
        """Set a nickname mapping, e.g. 'my browser' → 'Firefox'."""
        self.store.remember("nicknames", nickname.lower(), actual_name)

    def resolve_nickname(self, text):
        """Replace known nicknames in text with actual names."""
        nicknames = self.store.get_category("nicknames")
        if not nicknames:
            return text
        result = text
        for nick, actual in nicknames.items():
            if nick in result.lower():
                import re
                result = re.sub(re.escape(nick), actual, result, flags=re.IGNORECASE)
        return result

    # --- Response length preference (Phase 10) ---

    def track_response_preference(self, short):
        """Track whether user prefers shorter or longer responses."""
        key = "short_count" if short else "long_count"
        current = int(self.store.recall("preferences", key) or "0")
        self.store.remember("preferences", key, str(current + 1))

    def get_preferred_length(self):
        """Returns 'short', 'normal', or 'detailed' based on tracked preferences."""
        short = int(self.store.recall("preferences", "short_count") or "0")
        long = int(self.store.recall("preferences", "long_count") or "0")
        if short > long + 3:
            return "short"
        if long > short + 3:
            return "detailed"
        return "normal"


class HabitTracker:
    """Detect temporal usage patterns."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def get_typical_actions(self, hour=None, day=None):
        """What does the user typically do at this time/day?"""
        now = datetime.now()
        h = hour if hour is not None else now.hour
        d = day if day is not None else now.weekday()

        c = self.store._conn.cursor()
        c.execute("""
            SELECT action, entity, COUNT(*) as cnt FROM usage_log
            WHERE hour = ? AND day_of_week = ?
            GROUP BY action, entity
            ORDER BY cnt DESC
            LIMIT 5
        """, (h, d))

        return [
            {"action": row[0], "entity": row[1], "frequency": row[2]}
            for row in c.fetchall()
        ]

    def get_daily_summary(self):
        """What has the user done today?"""
        today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        c = self.store._conn.cursor()
        c.execute("""
            SELECT action, entity, COUNT(*) as cnt FROM usage_log
            WHERE timestamp >= ?
            GROUP BY action, entity
            ORDER BY cnt DESC
        """, (today_start,))

        return [
            {"action": row[0], "entity": row[1], "count": row[2]}
            for row in c.fetchall()
        ]

    def detect_routine(self, min_occurrences=5):
        """Find repeated command patterns at the current hour/day."""
        habits = self.get_typical_actions()
        return [h for h in habits if h["frequency"] >= min_occurrences]

    def suggest_proactive_actions(self):
        """Suggest actions based on current time patterns."""
        habits = self.get_typical_actions()
        suggestions = []
        for habit in habits:
            if habit["frequency"] >= 3:  # At least 3 occurrences to suggest
                if habit["action"] == "open_app" and habit["entity"]:
                    suggestions.append(f"Want me to open {habit['entity']}? You usually do around this time.")
                elif habit["action"] == "weather":
                    suggestions.append("Want me to check the weather? You usually ask around now.")
        return suggestions
