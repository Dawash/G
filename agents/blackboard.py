"""
Blackboard — Shared state for multi-agent communication.

All agents read/write to this shared memory space. It combines:
  - In-memory dict for fast ephemeral state (current plan, scores, etc.)
  - SQLite for persistent cross-session data (reflexions, skills)
  - TF-IDF vector index for semantic retrieval of past experiences

Design: Blackboard pattern (AI classic) — simple, zero-dependency,
works with any LLM size. No vector DB needed.
"""

import json
import logging
import os
import threading
import time
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from embeddings import VectorStore
    _HAS_VECTOR_STORE = True
except ImportError:
    _HAS_VECTOR_STORE = False

logger = logging.getLogger(__name__)


@dataclass
class PlanNode:
    """A single node in the plan tree."""
    id: str
    description: str
    deps: list = field(default_factory=list)
    tool_hint: str = ""
    status: str = "pending"      # pending, in_progress, done, failed, skipped
    result: str = ""
    score: float = 0.0           # Critic score (0-100)
    children: list = field(default_factory=list)  # For tree branches
    attempts: int = 0
    max_attempts: int = 3
    takeover: bool = False       # Requires user intervention


@dataclass
class AgentMessage:
    """Message passed between agents via the blackboard."""
    sender: str          # "planner", "executor", "critic", "researcher", "memory"
    content: dict        # Arbitrary payload
    timestamp: float = field(default_factory=time.time)
    msg_type: str = "info"  # info, request, result, error


class Blackboard:
    """Shared state space for multi-agent collaboration.

    Thread-safe. All agents read/write here.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = {
            "goal": "",
            "plan": [],               # list[PlanNode]
            "current_step_idx": 0,
            "phase": "idle",          # idle, planning, executing, critiquing, researching, done
            "action_history": [],     # list[dict] — tool calls + results
            "observations": [],       # list[dict] — screen states
            "critic_scores": [],      # list[dict] — {step_id, score, reason}
            "research_results": [],   # list[dict] — web findings
            "reflexions": [],         # list[str] — failure lessons
            "messages": [],           # list[AgentMessage] — inter-agent comms
            "metadata": {},           # arbitrary k/v
            "checkpoints": [],        # saved states for rollback
            "start_time": 0.0,
            "total_llm_calls": 0,
            "total_tool_calls": 0,
            "errors": [],
        }
        # TF-IDF index for semantic retrieval
        self._doc_index = {}   # doc_id -> {tokens: {term: tf}, text: str}
        self._idf_cache = {}
        self._doc_counter = 0
        # Vector store for semantic retrieval (enhancement over TF-IDF)
        self._vectors = None
        if _HAS_VECTOR_STORE:
            try:
                _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                self._vectors = VectorStore(
                    persist_dir=_base,
                    prefix="blackboard_vectors",
                )
                logger.debug("Blackboard: VectorStore initialized")
            except Exception as e:
                logger.warning(f"Blackboard: VectorStore init failed: {e}")
                self._vectors = None

    # --- Core State Access ---

    def get(self, key, default=None):
        with self._lock:
            return self._state.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._state[key] = value

    def update(self, updates: dict):
        with self._lock:
            self._state.update(updates)

    def append(self, key, value):
        with self._lock:
            if key not in self._state:
                self._state[key] = []
            self._state[key].append(value)

    # --- Plan Management ---

    def set_plan(self, nodes: list):
        """Set the execution plan (list of PlanNode)."""
        with self._lock:
            self._state["plan"] = nodes
            self._state["current_step_idx"] = 0

    def get_current_step(self) -> PlanNode | None:
        with self._lock:
            plan = self._state["plan"]
            idx = self._state["current_step_idx"]
            if idx < len(plan):
                return plan[idx]
            return None

    def advance_step(self):
        with self._lock:
            self._state["current_step_idx"] += 1

    def get_ready_steps(self) -> list:
        """Get steps whose dependencies are all done."""
        with self._lock:
            plan = self._state["plan"]
            done_ids = {n.id for n in plan if n.status == "done"}
            return [
                n for n in plan
                if n.status == "pending"
                and all(d in done_ids for d in n.deps)
            ]

    def mark_step(self, step_id: str, status: str, result: str = "", score: float = 0.0):
        with self._lock:
            for node in self._state["plan"]:
                if node.id == step_id:
                    node.status = status
                    node.result = result
                    node.score = score
                    node.attempts += 1
                    break

    def get_plan_progress(self) -> dict:
        with self._lock:
            plan = self._state["plan"]
            if not plan:
                return {"total": 0, "done": 0, "failed": 0, "pending": 0, "pct": 0}
            done = sum(1 for n in plan if n.status == "done")
            failed = sum(1 for n in plan if n.status == "failed")
            pending = sum(1 for n in plan if n.status in ("pending", "in_progress"))
            return {
                "total": len(plan),
                "done": done,
                "failed": failed,
                "pending": pending,
                "pct": int(done / len(plan) * 100) if plan else 0,
            }

    # --- Inter-Agent Messaging ---

    def post_message(self, sender: str, content: dict, msg_type: str = "info"):
        msg = AgentMessage(sender=sender, content=content, msg_type=msg_type)
        with self._lock:
            self._state["messages"].append(msg)

    def get_messages(self, sender: str = None, msg_type: str = None, limit: int = 10) -> list:
        with self._lock:
            msgs = self._state["messages"]
            if sender:
                msgs = [m for m in msgs if m.sender == sender]
            if msg_type:
                msgs = [m for m in msgs if m.msg_type == msg_type]
            return msgs[-limit:]

    # --- Action History ---

    def log_action(self, tool_name: str, args: dict, result: str, success: bool, duration: float = 0):
        entry = {
            "tool": tool_name,
            "args": args,
            "result": result[:500],
            "success": success,
            "duration": duration,
            "timestamp": time.time(),
        }
        with self._lock:
            self._state["action_history"].append(entry)
            self._state["total_tool_calls"] += 1

    def get_recent_actions(self, n: int = 5) -> list:
        with self._lock:
            return self._state["action_history"][-n:]

    def get_action_summary(self) -> str:
        """Human-readable summary of what's been done."""
        with self._lock:
            actions = self._state["action_history"]
        if not actions:
            return "No actions taken yet."
        lines = []
        for i, a in enumerate(actions[-10:], 1):
            status = "OK" if a["success"] else "FAIL"
            lines.append(f"  {i}. {a['tool']}({_fmt_args(a['args'])}) -> {status}: {a['result'][:80]}")
        return "\n".join(lines)

    # --- Checkpointing ---

    def checkpoint(self):
        """Save current state for rollback."""
        import copy
        with self._lock:
            snap = {
                "plan": copy.deepcopy(self._state["plan"]),
                "current_step_idx": self._state["current_step_idx"],
                "action_count": len(self._state["action_history"]),
                "timestamp": time.time(),
            }
            self._state["checkpoints"].append(snap)
            # Keep max 5 checkpoints
            if len(self._state["checkpoints"]) > 5:
                self._state["checkpoints"] = self._state["checkpoints"][-5:]

    def rollback(self) -> bool:
        """Restore last checkpoint. Returns True if successful."""
        import copy
        with self._lock:
            checkpoints = self._state["checkpoints"]
            if not checkpoints:
                return False
            snap = checkpoints.pop()
            self._state["plan"] = copy.deepcopy(snap["plan"])
            self._state["current_step_idx"] = snap["current_step_idx"]
            # Trim action history to checkpoint point
            self._state["action_history"] = self._state["action_history"][:snap["action_count"]]
            logger.info(f"Blackboard rolled back to checkpoint (step {snap['current_step_idx']})")
            return True

    # --- TF-IDF Vector Memory (for semantic retrieval) ---

    def index_document(self, doc_id: str, text: str):
        """Add a document to the TF-IDF index (and vector store if available)."""
        tokens = _tokenize(text)
        if not tokens:
            return
        tf = defaultdict(float)
        for t in tokens:
            tf[t] += 1
        # Normalize
        max_tf = max(tf.values())
        for t in tf:
            tf[t] = tf[t] / max_tf
        with self._lock:
            self._doc_index[doc_id] = {"tokens": dict(tf), "text": text}
            self._idf_cache.clear()  # Invalidate
            self._doc_counter += 1
        # Also add to vector store for semantic search
        if self._vectors is not None:
            try:
                self._vectors.add(doc_id, text)
            except Exception as e:
                logger.debug(f"Blackboard VectorStore add failed for '{doc_id}': {e}")

    def search_similar(self, query: str, top_k: int = 3) -> list:
        """Find documents most similar to query.

        Tries FAISS vector search first, falls back to TF-IDF cosine.
        """
        if not query:
            return []

        # Try vector search first
        if self._vectors is not None:
            try:
                vec_results = self._vectors.search(query, top_k=top_k, min_similarity=0.1)
                if vec_results:
                    return vec_results
            except Exception as e:
                logger.debug(f"Blackboard vector search failed, falling back to TF-IDF: {e}")

        # Fall back to TF-IDF
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        with self._lock:
            if not self._doc_index:
                return []
            # Compute IDF
            n_docs = len(self._doc_index)
            idf = {}
            for term in set(query_tokens):
                df = sum(1 for doc in self._doc_index.values() if term in doc["tokens"])
                idf[term] = math.log((n_docs + 1) / (df + 1)) + 1

            # Query vector
            q_tf = defaultdict(float)
            for t in query_tokens:
                q_tf[t] += 1
            max_q = max(q_tf.values())
            q_vec = {t: (q_tf[t] / max_q) * idf.get(t, 1) for t in q_tf}

            # Score each document
            results = []
            for doc_id, doc in self._doc_index.items():
                d_vec = {t: tf * idf.get(t, 1) for t, tf in doc["tokens"].items()}
                # Cosine similarity
                dot = sum(q_vec.get(t, 0) * d_vec.get(t, 0) for t in set(list(q_vec) + list(d_vec)))
                mag_q = math.sqrt(sum(v**2 for v in q_vec.values())) or 1
                mag_d = math.sqrt(sum(v**2 for v in d_vec.values())) or 1
                sim = dot / (mag_q * mag_d)
                if sim > 0.1:
                    results.append({"id": doc_id, "text": doc["text"], "similarity": sim})

            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:top_k]

    # --- Serialization ---

    def to_summary(self) -> str:
        """Compact summary for LLM context injection."""
        with self._lock:
            goal = self._state["goal"]
            progress = self.get_plan_progress()
            recent = self.get_action_summary()
            errors = self._state["errors"][-3:]
            reflexions = self._state["reflexions"][-3:]

        parts = [f"Goal: {goal}"]
        parts.append(f"Progress: {progress['done']}/{progress['total']} steps "
                      f"({progress['pct']}% complete, {progress['failed']} failed)")
        if recent != "No actions taken yet.":
            parts.append(f"Recent actions:\n{recent}")
        if errors:
            parts.append(f"Recent errors: {'; '.join(str(e)[:80] for e in errors)}")
        if reflexions:
            parts.append(f"Lessons learned: {'; '.join(reflexions[-3:])}")
        return "\n".join(parts)


# --- Helpers ---

def _tokenize(text: str) -> list:
    """Simple whitespace + lowercase tokenizer."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = text.split()
    # Remove stopwords
    _STOP = frozenset([
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "out", "off", "over",
        "under", "again", "further", "then", "once", "it", "its", "this",
        "that", "these", "those", "i", "me", "my", "we", "our", "you", "your",
        "he", "him", "his", "she", "her", "they", "them", "their", "and",
        "but", "or", "not", "no", "so", "if", "up", "all", "each", "every",
    ])
    return [t for t in tokens if t not in _STOP and len(t) > 1]


def _fmt_args(args: dict) -> str:
    """Format tool args for display."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 30:
            sv = sv[:27] + "..."
        parts.append(f"{k}={sv}")
    return ", ".join(parts)
