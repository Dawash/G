"""
Semantic Memory — Knowledge Graph (NetworkX) + Vector Store (FAISS).

Knowledge Graph: entities, relationships, confidence decay over time.
Vector Store: FAISS index for semantic similarity search over episodes/skills.
Falls back gracefully if networkx or faiss not installed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_NX_AVAILABLE = False
_FAISS_AVAILABLE = False
_NP_AVAILABLE = False

try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    pass

try:
    import numpy as np
    _NP_AVAILABLE = True
except ImportError:
    pass

try:
    import faiss  # type: ignore
    _FAISS_AVAILABLE = True
except ImportError:
    pass

GRAPH_PATH = os.path.join("data", "knowledge_graph.pkl")
VECTOR_PATH = os.path.join("data", "vector_store.pkl")
EMBED_DIM = 384  # MiniLM-L6 / fallback hash embedding dimension


# ── Embedding ──────────────────────────────────────────────────────────────────

_embed_model = None
_embed_lock = threading.Lock()


def _get_embedding_fn():
    """Lazy-load sentence-transformers if available, else use hash embedding."""
    global _embed_model
    with _embed_lock:
        if _embed_model is not None:
            return _embed_model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Semantic memory: using SentenceTransformer embeddings")
        except Exception:
            _embed_model = _hash_embed
            logger.info("Semantic memory: using hash-based embeddings (install sentence-transformers for better quality)")
        return _embed_model


def _hash_embed(text: str) -> "np.ndarray":
    """Deterministic hash-based embedding fallback (no ML required)."""
    import hashlib
    if not _NP_AVAILABLE:
        return None
    h = hashlib.sha256(text.encode()).digest()
    vec = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
    # Repeat to fill EMBED_DIM
    reps = math.ceil(EMBED_DIM / len(vec))
    vec = np.tile(vec, reps)[:EMBED_DIM]
    # Normalize
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


def embed(text: str) -> Optional["np.ndarray"]:
    """Embed text into a float32 vector of shape (EMBED_DIM,)."""
    if not _NP_AVAILABLE:
        return None
    try:
        fn = _get_embedding_fn()
        if fn is _hash_embed:
            return _hash_embed(text)
        result = fn.encode([text], convert_to_numpy=True, normalize_embeddings=True)
        return result[0].astype(np.float32)
    except Exception as e:
        logger.debug(f"Embed error: {e}")
        return None


# ── Knowledge Graph ────────────────────────────────────────────────────────────

@dataclass
class Entity:
    name: str
    etype: str = "concept"       # concept / person / app / fact / action
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attributes: Dict = field(default_factory=dict)


@dataclass
class Relation:
    source: str
    target: str
    rel_type: str                 # uses / likes / knows / triggers / follows
    weight: float = 1.0
    created_at: float = field(default_factory=time.time)


class KnowledgeGraph:
    """NetworkX-backed entity-relationship graph with confidence decay."""

    DECAY_RATE = 0.01  # per day
    MIN_CONFIDENCE = 0.1

    def __init__(self, path: str = GRAPH_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        if _NX_AVAILABLE:
            self._g: "nx.DiGraph | None" = nx.DiGraph()
        else:
            self._g = None
        self._load()

    # ── Persistence ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not _NX_AVAILABLE or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "rb") as f:
                self._g = pickle.load(f)
            logger.debug(f"Knowledge graph loaded: {self._g.number_of_nodes()} nodes")
        except Exception as e:
            logger.warning(f"Knowledge graph load failed: {e}")
            self._g = nx.DiGraph()

    def save(self) -> None:
        if not _NX_AVAILABLE or self._g is None:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            with open(self._path, "wb") as f:
                pickle.dump(self._g, f)
        except Exception as e:
            logger.warning(f"Knowledge graph save failed: {e}")

    # ── Mutation ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, etype: str = "concept",
                   confidence: float = 1.0, **attrs) -> None:
        if not _NX_AVAILABLE or self._g is None:
            return
        with self._lock:
            n = name.lower().strip()
            if self._g.has_node(n):
                # Update confidence upward on re-encounter
                old = self._g.nodes[n].get("confidence", 0.5)
                self._g.nodes[n]["confidence"] = min(1.0, old + 0.1)
                self._g.nodes[n]["updated_at"] = time.time()
            else:
                self._g.add_node(n, etype=etype, confidence=confidence,
                                 created_at=time.time(), updated_at=time.time(),
                                 **attrs)

    def add_relation(self, source: str, target: str, rel_type: str,
                     weight: float = 1.0) -> None:
        if not _NX_AVAILABLE or self._g is None:
            return
        with self._lock:
            s, t = source.lower().strip(), target.lower().strip()
            self.add_entity(s)
            self.add_entity(t)
            if self._g.has_edge(s, t):
                self._g[s][t]["weight"] = min(5.0, self._g[s][t].get("weight", 1.0) + 0.5)
            else:
                self._g.add_edge(s, t, rel_type=rel_type, weight=weight,
                                 created_at=time.time())

    # ── Query ───────────────────────────────────────────────────────────────

    def get_related(self, entity: str, depth: int = 2,
                    min_confidence: float = 0.3) -> List[str]:
        """Return entities within `depth` hops with sufficient confidence."""
        if not _NX_AVAILABLE or self._g is None:
            return []
        with self._lock:
            n = entity.lower().strip()
            if not self._g.has_node(n):
                return []
            try:
                subgraph = nx.ego_graph(self._g, n, radius=depth)
                return [
                    node for node in subgraph.nodes
                    if node != n and
                    subgraph.nodes[node].get("confidence", 0) >= min_confidence
                ]
            except Exception:
                return []

    def get_entity(self, name: str) -> Optional[Dict]:
        if not _NX_AVAILABLE or self._g is None:
            return None
        with self._lock:
            n = name.lower().strip()
            if self._g.has_node(n):
                return dict(self._g.nodes[n])
            return None

    def find_path(self, source: str, target: str) -> List[str]:
        """Return shortest path between two entities."""
        if not _NX_AVAILABLE or self._g is None:
            return []
        with self._lock:
            s, t = source.lower().strip(), target.lower().strip()
            try:
                return nx.shortest_path(self._g, s, t)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return []

    def most_connected(self, top_n: int = 10) -> List[Tuple[str, int]]:
        """Return top entities by in-degree (most referenced)."""
        if not _NX_AVAILABLE or self._g is None:
            return []
        with self._lock:
            return sorted(self._g.in_degree(), key=lambda x: x[1], reverse=True)[:top_n]

    def stats(self) -> Dict:
        if not _NX_AVAILABLE or self._g is None:
            return {"nodes": 0, "edges": 0, "available": False}
        with self._lock:
            return {
                "nodes": self._g.number_of_nodes(),
                "edges": self._g.number_of_edges(),
                "available": True,
            }

    # ── Decay ───────────────────────────────────────────────────────────────

    def apply_decay(self) -> None:
        """Reduce confidence of old entities. Call periodically."""
        if not _NX_AVAILABLE or self._g is None:
            return
        with self._lock:
            now = time.time()
            to_remove = []
            for node, data in self._g.nodes(data=True):
                age_days = (now - data.get("updated_at", now)) / 86400
                new_conf = data.get("confidence", 1.0) * math.exp(-self.DECAY_RATE * age_days)
                if new_conf < self.MIN_CONFIDENCE:
                    to_remove.append(node)
                else:
                    self._g.nodes[node]["confidence"] = new_conf
            self._g.remove_nodes_from(to_remove)
            if to_remove:
                logger.debug(f"Decay removed {len(to_remove)} low-confidence entities")


# ── Vector Store ───────────────────────────────────────────────────────────────

@dataclass
class VectorEntry:
    id: int
    text: str
    source: str          # "episode" / "skill" / "fact"
    source_id: int = 0
    created_at: float = field(default_factory=time.time)
    metadata: Dict = field(default_factory=dict)


class VectorStore:
    """FAISS flat-L2 index for semantic similarity search.

    Falls back to brute-force cosine similarity if FAISS unavailable.
    """

    def __init__(self, path: str = VECTOR_PATH, dim: int = EMBED_DIM) -> None:
        self._path = path
        self._dim = dim
        self._lock = threading.RLock()
        self._entries: List[VectorEntry] = []
        self._next_id = 0
        self._index = None
        self._dirty = False
        self._load()

    def _build_faiss_index(self) -> None:
        if not _FAISS_AVAILABLE or not _NP_AVAILABLE:
            return
        if not self._entries:
            self._index = faiss.IndexFlatIP(self._dim)  # inner product on normalized vecs
            return
        vecs = []
        for entry in self._entries:
            v = entry.metadata.get("_vec")
            if v is not None:
                vecs.append(v)
            else:
                vecs.append(np.zeros(self._dim, dtype=np.float32))
        mat = np.stack(vecs).astype(np.float32)
        idx = faiss.IndexFlatIP(self._dim)
        idx.add(mat)
        self._index = idx

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "rb") as f:
                data = pickle.load(f)
            self._entries = data.get("entries", [])
            self._next_id = data.get("next_id", len(self._entries))
            self._build_faiss_index()
            logger.debug(f"Vector store loaded: {len(self._entries)} entries")
        except Exception as e:
            logger.warning(f"Vector store load failed: {e}")

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            data = {"entries": self._entries, "next_id": self._next_id}
            with open(self._path, "wb") as f:
                pickle.dump(data, f)
            self._dirty = False
        except Exception as e:
            logger.warning(f"Vector store save failed: {e}")

    # ── Insertion ────────────────────────────────────────────────────────────

    def add(self, text: str, source: str, source_id: int = 0,
            metadata: Optional[Dict] = None) -> int:
        """Embed and store text. Returns entry id."""
        with self._lock:
            vec = embed(text)
            meta = metadata or {}
            if vec is not None:
                meta["_vec"] = vec

            entry = VectorEntry(
                id=self._next_id, text=text, source=source,
                source_id=source_id, metadata=meta,
            )
            self._entries.append(entry)
            self._next_id += 1
            self._dirty = True

            # Append to FAISS index if available
            if _FAISS_AVAILABLE and _NP_AVAILABLE and vec is not None:
                if self._index is None:
                    self._build_faiss_index()
                else:
                    self._index.add(vec.reshape(1, -1))

            return entry.id

    # ── Search ───────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5,
               source_filter: Optional[str] = None) -> List[Tuple[VectorEntry, float]]:
        """Return top-k entries by semantic similarity."""
        with self._lock:
            if not self._entries:
                return []

            q_vec = embed(query)
            if q_vec is None or not _NP_AVAILABLE:
                return self._text_fallback(query, top_k, source_filter)

            entries = self._entries if source_filter is None else [
                e for e in self._entries if e.source == source_filter
            ]
            if not entries:
                return []

            if _FAISS_AVAILABLE and self._index is not None and source_filter is None:
                return self._faiss_search(q_vec, top_k, entries)
            return self._brute_search(q_vec, top_k, entries)

    def _faiss_search(self, q_vec: "np.ndarray", top_k: int,
                      entries: List[VectorEntry]) -> List[Tuple[VectorEntry, float]]:
        try:
            q = q_vec.reshape(1, -1)
            k = min(top_k, len(entries))
            scores, ids = self._index.search(q, k)
            results = []
            for score, idx in zip(scores[0], ids[0]):
                if 0 <= idx < len(entries):
                    results.append((entries[idx], float(score)))
            return results
        except Exception as e:
            logger.debug(f"FAISS search error: {e}")
            return self._brute_search(q_vec, top_k, entries)

    def _brute_search(self, q_vec: "np.ndarray", top_k: int,
                      entries: List[VectorEntry]) -> List[Tuple[VectorEntry, float]]:
        scored = []
        for entry in entries:
            v = entry.metadata.get("_vec")
            if v is not None:
                score = float(np.dot(q_vec, v))
            else:
                score = 0.0
            scored.append((entry, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _text_fallback(self, query: str, top_k: int,
                       source_filter: Optional[str]) -> List[Tuple[VectorEntry, float]]:
        """Simple substring match fallback when embeddings unavailable."""
        q = query.lower()
        entries = self._entries if source_filter is None else [
            e for e in self._entries if e.source == source_filter
        ]
        scored = [(e, float(q in e.text.lower())) for e in entries]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(e, s) for e, s in scored if s > 0][:top_k]

    def stats(self) -> Dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "faiss_available": _FAISS_AVAILABLE,
                "numpy_available": _NP_AVAILABLE,
                "index_size": self._index.ntotal if (self._index and _FAISS_AVAILABLE) else 0,
            }


# ── Semantic facade ────────────────────────────────────────────────────────────

class _SemanticFacade:
    """Thin wrapper around KnowledgeGraph that exposes the ``add_fact`` /
    ``get_facts_about`` API expected by integration tests and external callers.

    This object is exposed as the module-level ``semantic`` singleton.
    """

    def __init__(self, kg: "KnowledgeGraph") -> None:
        self._kg = kg

    # Proxy all KnowledgeGraph attributes transparently
    def __getattr__(self, name: str):
        return getattr(self._kg, name)

    def add_fact(self, subject: str, predicate: str, obj: str,
                 confidence: float = 1.0) -> None:
        """Store a (subject, predicate, object) triple in the knowledge graph."""
        self._kg.add_entity(subject, etype="concept", confidence=confidence)
        self._kg.add_entity(obj, etype="concept", confidence=confidence)
        self._kg.add_relation(subject, obj, rel_type=predicate, weight=confidence)

    def get_facts_about(self, entity: str) -> List[Dict]:
        """Return all outgoing edges from ``entity`` as fact dicts."""
        if not _NX_AVAILABLE or self._kg._g is None:
            return []
        with self._kg._lock:
            n = entity.lower().strip()
            if not self._kg._g.has_node(n):
                return []
            facts = []
            for _, target, data in self._kg._g.out_edges(n, data=True):
                facts.append({
                    "subject": n,
                    "predicate": data.get("rel_type", "related_to"),
                    "object": target,
                    "weight": data.get("weight", 1.0),
                })
            return facts


# ── Module-level singletons ────────────────────────────────────────────────────

knowledge_graph = KnowledgeGraph()
vector_store = VectorStore()

# ``semantic`` is the integration-test-friendly alias for the knowledge graph
semantic = _SemanticFacade(knowledge_graph)
