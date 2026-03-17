"""
MemoryAPI — unified entry point for the 3-layer memory system.

    Layer 1: WorkingMemory    — short-term RAM buffer (6-20 msgs, topic tracking)
    Layer 2: EpisodicMemory   — SQLite archive (episodes, skills, failures, user_facts)
    Layer 3: SemanticMemory   — NetworkX knowledge graph + FAISS vector store

Usage (singleton):
    from memory.memory_api import memory
    memory.add_turn("user", "open spotify")
    memory.log_episode("open spotify", response="Opening Spotify", success=True)
    memory.learn_entity("spotify", etype="app")
    results = memory.recall("spotify last week")
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from memory.working_memory import WorkingMemory, ActiveTask, working_memory
from memory.episodic_memory import EpisodicMemory, Episode, Skill, episodic
from memory.semantic_memory import (
    KnowledgeGraph, VectorStore, VectorEntry,
    knowledge_graph, vector_store,
)

logger = logging.getLogger(__name__)


class MemoryAPI:
    """Single interface across all three memory layers."""

    def __init__(
        self,
        wm: WorkingMemory = working_memory,
        ep: EpisodicMemory = episodic,
        kg: KnowledgeGraph = knowledge_graph,
        vs: VectorStore = vector_store,
    ) -> None:
        self._wm = wm
        self._ep = ep
        self._kg = kg
        self._vs = vs
        self._save_lock = threading.Lock()
        self._last_save = 0.0
        self._save_interval = 120.0  # seconds between auto-saves

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 1 — Working Memory
    # ══════════════════════════════════════════════════════════════════════════

    def add_turn(self, role: str, content: str) -> None:
        """Add a conversation turn to the sliding window."""
        self._wm.add_message(role, content)

    def get_context(self, include_timestamps: bool = False) -> List[Dict]:
        """Return current conversation window."""
        return self._wm.get_messages(include_timestamps=include_timestamps)

    def get_last_n(self, n: int) -> List[Dict]:
        return self._wm.get_last_n(n)

    @property
    def current_topic(self) -> str:
        return self._wm.current_topic

    @property
    def active_task(self) -> ActiveTask:
        return self._wm.active_task

    def start_task(self, goal: str, plan: List[str]) -> None:
        self._wm.start_task(goal, plan)

    def complete_step(self, result: Optional[Dict] = None) -> None:
        self._wm.complete_step(result)

    def clear_working(self) -> None:
        self._wm.clear()

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 2 — Episodic Memory
    # ══════════════════════════════════════════════════════════════════════════

    def log_episode(self, user_input: str, response: str = "",
                    tools: Optional[List[str]] = None, success: bool = True,
                    topic: str = "", emotion: str = "neutral",
                    duration_ms: int = 0) -> int:
        """Persist an interaction episode. Also indexes into vector store."""
        ep_id = self._ep.log_episode(
            user_input=user_input, response=response,
            tools=tools, success=success,
            topic=topic or self.current_topic,
            emotion=emotion, duration_ms=duration_ms,
        )
        # Cross-index into vector store
        if user_input:
            self._vs.add(
                text=f"{user_input} {response}".strip(),
                source="episode",
                source_id=ep_id,
                metadata={"topic": topic, "success": success},
            )
        self._maybe_save()
        return ep_id

    def search_episodes(self, query: str, limit: int = 10) -> List[Episode]:
        """Full-text search via FTS5 / LIKE."""
        return self._ep.search(query, limit=limit)

    def recent_episodes(self, limit: int = 20) -> List[Episode]:
        return self._ep.get_recent(limit=limit)

    # ── Skills ────────────────────────────────────────────────────────────────

    def learn_skill(self, goal: str, tool_sequence: List[Dict]) -> int:
        """Store a successful tool sequence as a reusable skill."""
        skill_id = self._ep.learn_skill(goal, tool_sequence)
        # Cross-index goal into vector store for fuzzy recall
        self._vs.add(text=goal, source="skill", source_id=skill_id)
        self._maybe_save()
        return skill_id

    def find_skill(self, goal: str, min_reliability: float = 0.7) -> Optional[Skill]:
        """Find a matching skill by exact/partial goal text."""
        return self._ep.find_skill(goal, min_reliability=min_reliability)

    def find_skill_semantic(self, goal: str,
                            min_score: float = 0.7) -> Optional[Skill]:
        """Find a skill via vector similarity (broader match than text search)."""
        results = self._vs.search(goal, top_k=3, source_filter="skill")
        for entry, score in results:
            if score >= min_score:
                skill = self._ep.find_skill(entry.text)
                if skill:
                    return skill
        return None

    def mark_skill_success(self, skill_id: int) -> None:
        self._ep.mark_skill_success(skill_id)

    def mark_skill_failure(self, skill_id: int) -> None:
        self._ep.mark_skill_failure(skill_id)

    # ── Failures ─────────────────────────────────────────────────────────────

    def log_failure(self, goal: str, error: str,
                    context: str = "", lesson: str = "") -> None:
        self._ep.log_failure(goal, error, context=context, lesson=lesson)

    def get_failures_for(self, goal: str, limit: int = 5) -> List[Dict]:
        return self._ep.get_failures_for(goal, limit=limit)

    # ── User facts ────────────────────────────────────────────────────────────

    def set_user_fact(self, key: str, value: str,
                      source: str = "inferred", confidence: float = 0.5) -> None:
        self._ep.set_user_fact(key, value, source=source, confidence=confidence)
        # Reflect in knowledge graph
        self._kg.add_entity(key, etype="fact", confidence=confidence)
        self._kg.add_entity(value, etype="concept")
        self._kg.add_relation(key, value, rel_type="has_value")

    def get_user_fact(self, key: str) -> Optional[str]:
        return self._ep.get_user_fact(key)

    def get_all_user_facts(self) -> Dict[str, str]:
        return self._ep.get_all_user_facts()

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 3 — Semantic Memory
    # ══════════════════════════════════════════════════════════════════════════

    def learn_entity(self, name: str, etype: str = "concept",
                     confidence: float = 1.0, **attrs) -> None:
        self._kg.add_entity(name, etype=etype, confidence=confidence, **attrs)

    def learn_relation(self, source: str, target: str, rel_type: str,
                       weight: float = 1.0) -> None:
        self._kg.add_relation(source, target, rel_type, weight=weight)

    def get_related(self, entity: str, depth: int = 2) -> List[str]:
        return self._kg.get_related(entity, depth=depth)

    def find_path(self, source: str, target: str) -> List[str]:
        return self._kg.find_path(source, target)

    def get_entity(self, name: str) -> Optional[Dict]:
        return self._kg.get_entity(name)

    def most_connected_entities(self, top_n: int = 10) -> List[Tuple[str, int]]:
        return self._kg.most_connected(top_n=top_n)

    # ── Vector recall ─────────────────────────────────────────────────────────

    def embed_and_store(self, text: str, source: str = "manual",
                        source_id: int = 0, metadata: Optional[Dict] = None) -> int:
        return self._vs.add(text, source=source, source_id=source_id, metadata=metadata)

    def recall(self, query: str, top_k: int = 5,
               source_filter: Optional[str] = None) -> List[Tuple[VectorEntry, float]]:
        """Semantic similarity search across all indexed content."""
        return self._vs.search(query, top_k=top_k, source_filter=source_filter)

    def recall_text(self, query: str, top_k: int = 5) -> List[str]:
        """Return plain text snippets from semantic recall."""
        return [entry.text for entry, _ in self.recall(query, top_k=top_k)]

    # ══════════════════════════════════════════════════════════════════════════
    # Cross-layer helpers
    # ══════════════════════════════════════════════════════════════════════════

    def learn_from_turn(self, user_input: str, response: str,
                        tools: Optional[List[str]] = None,
                        success: bool = True, duration_ms: int = 0) -> int:
        """One-call update: add to working memory + log episode + extract entities."""
        self.add_turn("user", user_input)
        self.add_turn("assistant", response)
        ep_id = self.log_episode(
            user_input=user_input, response=response,
            tools=tools, success=success, duration_ms=duration_ms,
        )
        self._extract_entities(user_input)
        return ep_id

    def _extract_entities(self, text: str) -> None:
        """Heuristic entity extraction: capitalize words, known types."""
        # Apps — words preceding "open", "close", "launch", etc.
        import re
        app_match = re.findall(
            r'(?:open|launch|start|close|stop)\s+([A-Z][a-zA-Z]+)', text
        )
        for app in app_match:
            self._kg.add_entity(app, etype="app")

        # Proper nouns (simple heuristic)
        proper = re.findall(r'\b([A-Z][a-z]{2,})\b', text)
        for word in proper[:5]:  # cap to avoid noise
            if len(word) > 3:
                self._kg.add_entity(word.lower(), etype="concept", confidence=0.6)

    def context_for_query(self, query: str) -> Dict:
        """Gather multi-layer context relevant to a query."""
        return {
            "working": self.get_context(),
            "topic": self.current_topic,
            "similar_episodes": [
                {"input": e.user_input, "response": e.response[:200]}
                for e in self.search_episodes(query, limit=3)
            ],
            "related_entities": self.get_related(query.split()[0] if query else ""),
            "semantic_hits": self.recall_text(query, top_k=3),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Stats & persistence
    # ══════════════════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        return {
            "episodic": self._ep.get_stats(),
            "graph": self._kg.stats(),
            "vectors": self._vs.stats(),
        }

    def save(self) -> None:
        """Flush semantic memory to disk (episodic saves on each write)."""
        with self._save_lock:
            self._kg.save()
            self._vs.save()
            self._last_save = time.time()

    def _maybe_save(self) -> None:
        if time.time() - self._last_save > self._save_interval:
            # Save in background to avoid blocking callers
            t = threading.Thread(target=self.save, daemon=True)
            t.start()

    def apply_decay(self) -> None:
        """Decay knowledge graph confidence. Call daily."""
        self._kg.apply_decay()
        self._kg.save()

    # ══════════════════════════════════════════════════════════════════════════
    # Compatibility aliases (for integration tests / external callers)
    # ══════════════════════════════════════════════════════════════════════════

    def add_message(self, role: str, content: str) -> None:
        """Alias for add_turn()."""
        self.add_turn(role, content)

    def get_messages(self, include_timestamps: bool = False) -> List[Dict]:
        """Alias for get_context()."""
        return self.get_context(include_timestamps=include_timestamps)

    def log(self, user_input: str, response: str = "",
            tools: Optional[List[str]] = None, topic: str = "") -> int:
        """Alias for log_episode()."""
        return self.log_episode(user_input=user_input, response=response,
                                tools=tools, topic=topic)

    def stats(self) -> Dict:
        """Alias for get_stats()."""
        return self.get_stats()

    @property
    def working(self) -> "WorkingMemory":
        """Return the underlying WorkingMemory instance."""
        return self._wm

    @property
    def episodic(self) -> "EpisodicMemory":
        """Return the underlying EpisodicMemory instance."""
        return self._ep


# Module-level singleton
memory = MemoryAPI()
