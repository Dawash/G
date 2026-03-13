"""
Vector Store — FAISS-backed semantic search with sentence-transformers.

Provides fast similarity search over text documents using dense embeddings.
Falls back to TF-IDF keyword matching if sentence-transformers or faiss-cpu
are not installed.

Architecture:
  VectorStore
    ├── add()      — encode text and add to FAISS index
    ├── search()   — find top-k similar documents
    ├── remove()   — remove a document by ID
    ├── save()     — persist index + metadata to disk
    ├── load()     — restore from disk
    └── count()    — number of indexed documents

Storage:
  - vectors.faiss     — FAISS index binary
  - vectors_meta.json — document IDs, texts, and metadata

Model: all-MiniLM-L6-v2 (384 dimensions, ~80MB, fast on CPU)
Lazy-loaded on first use to avoid slowing startup.
"""

import json
import logging
import math
import os
import re
import threading
from collections import Counter

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# TF-IDF fallback (copied from skills.py for standalone use)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Check for optional dependencies
# ---------------------------------------------------------------------------

_HAS_FAISS = False
_HAS_SENTENCE_TRANSFORMERS = False

try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    pass

try:
    import sentence_transformers  # noqa: F401
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    pass

_USE_VECTORS = _HAS_FAISS and _HAS_SENTENCE_TRANSFORMERS

if not _USE_VECTORS:
    _missing = []
    if not _HAS_FAISS:
        _missing.append("faiss-cpu")
    if not _HAS_SENTENCE_TRANSFORMERS:
        _missing.append("sentence-transformers")
    logger.info(
        f"VectorStore falling back to TF-IDF (missing: {', '.join(_missing)}). "
        f"Install with: pip install {' '.join(_missing)}"
    )


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class VectorStore:
    """FAISS-backed vector store with sentence-transformer embeddings.

    Falls back to TF-IDF keyword matching when FAISS or
    sentence-transformers are not installed.

    Args:
        persist_dir: Directory for saving index files.
        prefix: Filename prefix (allows multiple stores in one dir).
        model_name: Sentence-transformer model to use.
    """

    # Model dimensions for known models
    _MODEL_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
    }

    def __init__(
        self,
        persist_dir: str = _BASE_DIR,
        prefix: str = "vectors",
        model_name: str = "all-MiniLM-L6-v2",
    ):
        self._persist_dir = persist_dir
        self._prefix = prefix
        self._model_name = model_name
        self._dim = self._MODEL_DIMS.get(model_name, 384)
        self._lock = threading.Lock()

        # FAISS state (lazy-initialized)
        self._model = None         # SentenceTransformer instance
        self._index = None         # faiss.IndexFlatIP
        self._model_loaded = False

        # Document registry: maps position in FAISS index -> doc info
        # _id_to_pos[doc_id] = position in FAISS index
        # _pos_to_doc[position] = {id, text, metadata}
        self._id_to_pos: dict[str, int] = {}
        self._pos_to_doc: dict[int, dict] = {}
        self._next_pos: int = 0

        # TF-IDF fallback state
        self._tfidf_docs: dict[str, dict] = {}  # doc_id -> {text, tokens}

        # Try to load persisted data
        self._load_metadata()
        if _USE_VECTORS:
            self._load_faiss_index()

    # --- Lazy Model Loading ---

    def _ensure_model(self):
        """Load the sentence-transformer model on first use."""
        if self._model_loaded:
            return self._model is not None
        self._model_loaded = True
        if not _USE_VECTORS:
            return False
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {self._model_name}")
            self._model = SentenceTransformer(self._model_name)
            # Initialize FAISS index if not loaded from disk
            if self._index is None:
                self._index = faiss.IndexFlatIP(self._dim)
            logger.info(
                f"Embedding model loaded ({self._model_name}, dim={self._dim})"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to load embedding model: {e}")
            self._model = None
            return False

    def _encode(self, text: str):
        """Encode text to a normalized embedding vector."""
        if self._model is None:
            return None
        import numpy as np
        embedding = self._model.encode([text], normalize_embeddings=True)
        return np.array(embedding, dtype=np.float32)

    # --- Public API ---

    def add(self, doc_id: str, text: str, metadata: dict = None):
        """Encode text and add to the index.

        Args:
            doc_id: Unique document identifier.
            text: Text content to index.
            metadata: Optional metadata dict stored alongside the document.
        """
        if not doc_id or not text:
            return

        with self._lock:
            # Always update TF-IDF fallback
            tokens = _tokenize(text)
            self._tfidf_docs[doc_id] = {"text": text, "tokens": tokens}

            # Try vector index
            if self._ensure_model() and self._index is not None:
                # Remove old entry if updating
                if doc_id in self._id_to_pos:
                    self._remove_from_faiss(doc_id)

                vec = self._encode(text)
                if vec is not None:
                    pos = self._next_pos
                    self._index.add(vec)
                    self._id_to_pos[doc_id] = pos
                    self._pos_to_doc[pos] = {
                        "id": doc_id,
                        "text": text,
                        "metadata": metadata or {},
                    }
                    self._next_pos += 1

    def search(self, query: str, top_k: int = 5, min_similarity: float = 0.3) -> list:
        """Find documents most similar to the query.

        Args:
            query: Search query text.
            top_k: Maximum number of results.
            min_similarity: Minimum similarity threshold (0.0-1.0).

        Returns:
            List of dicts: [{id, text, similarity, metadata}, ...]
            Sorted by similarity descending.
        """
        if not query:
            return []

        with self._lock:
            # Try FAISS first
            if self._ensure_model() and self._index is not None and self._index.ntotal > 0:
                return self._search_faiss(query, top_k, min_similarity)
            # Fall back to TF-IDF
            return self._search_tfidf(query, top_k, min_similarity)

    def remove(self, doc_id: str):
        """Remove a document from the index.

        Args:
            doc_id: Document identifier to remove.
        """
        with self._lock:
            # Remove from TF-IDF
            self._tfidf_docs.pop(doc_id, None)

            # Remove from FAISS (mark as removed; rebuild on save)
            if doc_id in self._id_to_pos:
                self._remove_from_faiss(doc_id)

    def save(self):
        """Persist the index and metadata to disk."""
        with self._lock:
            self._save_metadata()
            if _USE_VECTORS and self._index is not None:
                self._save_faiss_index()

    def load(self):
        """Load the index and metadata from disk."""
        with self._lock:
            self._load_metadata()
            if _USE_VECTORS:
                self._load_faiss_index()

    def count(self) -> int:
        """Return the number of indexed documents."""
        with self._lock:
            if _USE_VECTORS and self._index is not None:
                return len(self._id_to_pos)
            return len(self._tfidf_docs)

    @property
    def using_vectors(self) -> bool:
        """Whether the store is using FAISS vectors (vs TF-IDF fallback)."""
        return _USE_VECTORS and self._model is not None

    # --- FAISS Internal Methods ---

    def _search_faiss(self, query: str, top_k: int, min_similarity: float) -> list:
        """Search using FAISS inner-product similarity."""
        vec = self._encode(query)
        if vec is None:
            return self._search_tfidf(query, top_k, min_similarity)

        # Search more than top_k to account for removed docs
        search_k = min(top_k * 2, self._index.ntotal)
        scores, indices = self._index.search(vec, search_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            doc = self._pos_to_doc.get(int(idx))
            if doc is None:
                continue  # Was removed
            sim = float(score)
            if sim >= min_similarity:
                results.append({
                    "id": doc["id"],
                    "text": doc["text"],
                    "similarity": round(sim, 4),
                    "metadata": doc.get("metadata", {}),
                })
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    def _remove_from_faiss(self, doc_id: str):
        """Mark a document as removed from the FAISS index.

        FAISS IndexFlatIP does not support true deletion, so we remove from
        our position map. The stale vector remains in the index until the
        next rebuild (on save).
        """
        pos = self._id_to_pos.pop(doc_id, None)
        if pos is not None:
            self._pos_to_doc.pop(pos, None)

    def _rebuild_faiss_index(self):
        """Rebuild the FAISS index from current documents (removes stale vectors)."""
        if not _USE_VECTORS or self._model is None:
            return
        import numpy as np

        docs = list(self._pos_to_doc.values())
        self._index = faiss.IndexFlatIP(self._dim)
        self._id_to_pos.clear()
        self._pos_to_doc.clear()
        self._next_pos = 0

        if not docs:
            return

        texts = [d["text"] for d in docs]
        embeddings = self._model.encode(texts, normalize_embeddings=True)
        embeddings = np.array(embeddings, dtype=np.float32)
        self._index.add(embeddings)

        for i, doc in enumerate(docs):
            self._id_to_pos[doc["id"]] = i
            self._pos_to_doc[i] = doc
        self._next_pos = len(docs)

    # --- TF-IDF Fallback ---

    def _search_tfidf(self, query: str, top_k: int, min_similarity: float) -> list:
        """Fallback search using TF-IDF cosine similarity."""
        query_tokens = _tokenize(query)
        if not query_tokens or not self._tfidf_docs:
            return []

        results = []
        for doc_id, doc in self._tfidf_docs.items():
            sim = _tfidf_similarity(query_tokens, doc["tokens"])
            if sim >= min_similarity:
                results.append({
                    "id": doc_id,
                    "text": doc["text"],
                    "similarity": round(sim, 4),
                    "metadata": {},
                })
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    # --- Persistence ---

    def _meta_path(self) -> str:
        return os.path.join(self._persist_dir, f"{self._prefix}_meta.json")

    def _faiss_path(self) -> str:
        return os.path.join(self._persist_dir, f"{self._prefix}.faiss")

    def _save_metadata(self):
        """Save document metadata and TF-IDF data to JSON."""
        data = {
            "docs": {},
            "next_pos": self._next_pos,
        }
        # Save all docs from pos_to_doc (FAISS-tracked)
        for pos, doc in self._pos_to_doc.items():
            data["docs"][str(pos)] = {
                "id": doc["id"],
                "text": doc["text"],
                "metadata": doc.get("metadata", {}),
            }
        # Save TF-IDF doc list for fallback restore
        data["tfidf_docs"] = {
            doc_id: {"text": d["text"]}
            for doc_id, d in self._tfidf_docs.items()
        }
        try:
            path = self._meta_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            logger.debug(f"VectorStore metadata saved ({len(data['docs'])} docs)")
        except Exception as e:
            logger.error(f"Failed to save vector metadata: {e}")

    def _load_metadata(self):
        """Load document metadata from JSON."""
        path = self._meta_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Restore position maps
            self._id_to_pos.clear()
            self._pos_to_doc.clear()
            for pos_str, doc in data.get("docs", {}).items():
                pos = int(pos_str)
                self._id_to_pos[doc["id"]] = pos
                self._pos_to_doc[pos] = doc
            self._next_pos = data.get("next_pos", 0)

            # Restore TF-IDF fallback
            self._tfidf_docs.clear()
            for doc_id, d in data.get("tfidf_docs", {}).items():
                text = d.get("text", "")
                self._tfidf_docs[doc_id] = {
                    "text": text,
                    "tokens": _tokenize(text),
                }
            logger.debug(
                f"VectorStore metadata loaded ({len(self._id_to_pos)} vector docs, "
                f"{len(self._tfidf_docs)} tfidf docs)"
            )
        except Exception as e:
            logger.error(f"Failed to load vector metadata: {e}")

    def _save_faiss_index(self):
        """Save FAISS index to disk, rebuilding to remove stale vectors."""
        if not _USE_VECTORS or self._index is None:
            return
        try:
            # Rebuild to remove stale (deleted) vectors
            stale_count = self._index.ntotal - len(self._id_to_pos)
            if stale_count > 0:
                logger.debug(f"Rebuilding FAISS index (removing {stale_count} stale vectors)")
                self._rebuild_faiss_index()

            path = self._faiss_path()
            faiss.write_index(self._index, path)
            logger.debug(f"FAISS index saved ({self._index.ntotal} vectors)")
        except Exception as e:
            logger.error(f"Failed to save FAISS index: {e}")

    def _load_faiss_index(self):
        """Load FAISS index from disk."""
        path = self._faiss_path()
        if not os.path.exists(path):
            return
        try:
            self._index = faiss.read_index(path)
            logger.debug(f"FAISS index loaded ({self._index.ntotal} vectors)")
        except Exception as e:
            logger.warning(f"Failed to load FAISS index: {e}")
            self._index = None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_store_instance: VectorStore | None = None
_store_lock = threading.Lock()


def get_store(persist_dir: str = _BASE_DIR, prefix: str = "vectors") -> VectorStore:
    """Get or create the singleton VectorStore instance.

    Args:
        persist_dir: Directory for persistence files.
        prefix: Filename prefix for this store.

    Returns:
        VectorStore singleton.
    """
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = VectorStore(persist_dir=persist_dir, prefix=prefix)
    return _store_instance
