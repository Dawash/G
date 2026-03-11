"""
Response cache for tool results.

TTL-based caching keyed on tool_name + serialized arguments.
Thread-safe via Lock.  Tracks hit/miss counters for metrics.
"""

import json
import logging
import time
import threading

logger = logging.getLogger(__name__)


class ResponseCache:
    """Thread-safe TTL cache for tool results."""

    def __init__(self):
        self._cache = {}          # key -> (result, timestamp, ttl)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, tool_name, arguments, ttl):
        """Return cached result if fresh, else None."""
        key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        with self._lock:
            entry = self._cache.get(key)
            if entry and (time.time() - entry[1]) < ttl:
                self._hits += 1
                logger.info(f"Cache hit: {tool_name}")
                return entry[0]
            if entry:
                del self._cache[key]
            self._misses += 1
        return None

    def set(self, tool_name, arguments, result, ttl=0):
        """Store a result in cache.

        Args:
            tool_name: Tool that produced the result.
            arguments: Arguments dict used for the call.
            result: The result to cache.
            ttl: TTL in seconds (stored for evict_expired; 0 = no auto-evict).
        """
        key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        with self._lock:
            self._cache[key] = (result, time.time(), ttl)

    def stats(self):
        """Return cache statistics.

        Returns:
            dict with keys: hits, misses, entries, hit_rate.
        """
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._cache),
                "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
            }

    def evict_expired(self):
        """Remove all expired entries. Returns count of evicted entries.

        Uses the TTL stored at set-time.  Entries stored with ttl=0
        are skipped (never auto-evicted).
        """
        now = time.time()
        evicted = 0
        with self._lock:
            expired_keys = []
            for key, entry in self._cache.items():
                # entry is (result, timestamp, ttl)
                ttl = entry[2] if len(entry) > 2 else 0
                if ttl > 0 and (now - entry[1]) >= ttl:
                    expired_keys.append(key)
            for key in expired_keys:
                del self._cache[key]
                evicted += 1
        if evicted:
            logger.debug(f"Cache evicted {evicted} expired entries")
        return evicted

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
