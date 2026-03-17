"""
Working Memory — short-term conversation buffer with topic tracking.

Smart sliding window: keeps 6-20 messages based on topic continuity.
Lives in RAM — not persisted.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ActiveTask:
    """Tracks a multi-step task in progress."""
    goal: str = ""
    plan: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    current_step: int = 0
    tool_results: List[Dict] = field(default_factory=list)
    started_at: float = 0.0
    status: str = "idle"  # idle / active / paused / completed / failed


class WorkingMemory:
    """Short-term conversation buffer with topic-aware sliding window."""

    MIN_WINDOW = 6
    MAX_WINDOW = 20

    _STOP = frozenset({
        "the", "a", "an", "is", "it", "to", "for", "and", "or", "in", "on",
        "what", "how", "can", "you", "me", "my", "i", "do", "this", "that",
        "of", "with", "about", "please", "hey", "hi", "was", "were", "be",
        "been", "being", "have", "has", "had", "will", "would", "could",
        "should", "may", "might", "shall", "not", "no", "yes", "just",
        "also", "very", "really", "so", "but", "if", "then", "than",
        "more", "most", "some", "any", "all", "each", "every",
    })

    def __init__(self) -> None:
        self._messages: List[Dict] = []
        self._current_topic: str = ""
        self._topic_keywords: Counter = Counter()
        self._topic_history: List[str] = []
        self._active_task: ActiveTask = ActiveTask()
        self._lock = threading.Lock()

    def add_message(self, role: str, content: str) -> None:
        with self._lock:
            self._messages.append({
                "role": role,
                "content": content,
                "timestamp": time.time(),
            })
            if role == "user":
                kw = self._extract_keywords(content)
                self._topic_keywords.update(kw)
                new_topic = self._detect_topic()
                if new_topic and new_topic != self._current_topic:
                    if self._current_topic:
                        self._topic_history.append(self._current_topic)
                        self._topic_history = self._topic_history[-10:]
                    self._current_topic = new_topic

            window = self._calculate_window_size()
            if len(self._messages) > window:
                self._messages = self._messages[-window:]

    def get_messages(self, include_timestamps: bool = False) -> List[Dict]:
        with self._lock:
            if include_timestamps:
                return [m.copy() for m in self._messages]
            return [{"role": m["role"], "content": m["content"]} for m in self._messages]

    def get_last_n(self, n: int) -> List[Dict]:
        with self._lock:
            return [{"role": m["role"], "content": m["content"]}
                    for m in self._messages[-n:]]

    @property
    def current_topic(self) -> str:
        with self._lock:
            return self._current_topic

    @property
    def active_task(self) -> ActiveTask:
        return self._active_task

    def start_task(self, goal: str, plan: List[str]) -> None:
        self._active_task = ActiveTask(
            goal=goal, plan=plan,
            started_at=time.time(), status="active",
        )

    def complete_step(self, result: Optional[Dict] = None) -> None:
        task = self._active_task
        if task.status == "active" and task.current_step < len(task.plan):
            task.completed_steps.append(task.plan[task.current_step])
            if result:
                task.tool_results.append(result)
            task.current_step += 1
            if task.current_step >= len(task.plan):
                task.status = "completed"

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._current_topic = ""
            self._topic_keywords.clear()
        self._active_task = ActiveTask()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _calculate_window_size(self) -> int:
        if len(self._messages) < self.MIN_WINDOW:
            return self.MAX_WINDOW
        recent = [m["content"] for m in self._messages[-6:] if m["role"] == "user"]
        if len(recent) < 2:
            return self.MAX_WINDOW
        kw_sets = [set(self._extract_keywords(msg)) for msg in recent]
        if len(kw_sets) >= 2:
            overlap = kw_sets[-1] & kw_sets[-2]
            if len(overlap) >= 2:
                return self.MAX_WINDOW
            elif len(overlap) >= 1:
                return 12
        return self.MIN_WINDOW

    def _extract_keywords(self, text: str) -> List[str]:
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        return [w for w in words if w not in self._STOP]

    def _detect_topic(self) -> str:
        if not self._topic_keywords:
            return ""
        most_common = self._topic_keywords.most_common(1)
        return most_common[0][0] if most_common else ""


# Module-level singleton
working_memory = WorkingMemory()
