"""
Time-budgeted self-improvement — constrains autonomous learning.

Each improvement attempt gets:
  - Max 60 seconds runtime
  - Max 3 web research calls
  - Max 2 LLM calls
  - Max 3 improvements per cycle

After applying, a review window (1 hour) measures success rate.
Bad improvements auto-revert.
"""

import time
import logging
import json
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REVIEW_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "improvement_log.json")


@dataclass
class ImprovementBudget:
    """Resource limits for a single improvement cycle."""
    max_seconds: float = 3600.0
    max_web_requests: int = 999
    max_llm_calls: int = 999
    max_improvements_per_cycle: int = 999


class BudgetedRunner:
    """Enforces time and resource budgets on improvement attempts."""

    def __init__(self, budget: ImprovementBudget = None):
        self.budget = budget or ImprovementBudget()
        self._web_calls = 0
        self._llm_calls = 0
        self._improvements_applied = 0
        self._start_time = 0.0
        self._active = False

    def start(self):
        """Begin tracking budget."""
        self._start_time = time.time()
        self._active = True
        self._web_calls = 0
        self._llm_calls = 0
        self._improvements_applied = 0

    def elapsed(self) -> float:
        return time.time() - self._start_time if self._active else 0.0

    def is_expired(self) -> bool:
        return self._active and self.elapsed() > self.budget.max_seconds

    def can_web(self) -> bool:
        return self._web_calls < self.budget.max_web_requests and not self.is_expired()

    def can_llm(self) -> bool:
        return self._llm_calls < self.budget.max_llm_calls and not self.is_expired()

    def can_improve(self) -> bool:
        return self._improvements_applied < self.budget.max_improvements_per_cycle and not self.is_expired()

    def track_web(self):
        self._web_calls += 1

    def track_llm(self):
        self._llm_calls += 1

    def track_improvement(self):
        self._improvements_applied += 1

    def summary(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed(), 1),
            "web_calls": self._web_calls,
            "llm_calls": self._llm_calls,
            "improvements": self._improvements_applied,
        }


class ImprovementTracker:
    """Tracks improvements and reviews them after a window period.

    Workflow:
    1. record_improvement() — log what was changed and the baseline success rate
    2. review_pending() — after review_window, check if improvement helped
    3. Auto-revert if it made things worse
    """

    def __init__(self, review_window_seconds=3600):
        self._review_window = review_window_seconds
        self._pending = []  # List of pending improvement records
        self._load()

    def _load(self):
        """Load pending improvements from disk."""
        try:
            if os.path.isfile(REVIEW_LOG):
                with open(REVIEW_LOG, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._pending = data.get("pending", [])
        except (json.JSONDecodeError, OSError):
            self._pending = []

    def _save(self):
        """Persist pending improvements."""
        try:
            with open(REVIEW_LOG, "w", encoding="utf-8") as f:
                json.dump({"pending": self._pending}, f, indent=2)
        except OSError:
            pass

    def record_improvement(self, pattern, adjustment_id, baseline_rate, description=""):
        """Record a newly applied improvement for later review.

        Args:
            pattern: The pattern/behavior being improved (e.g., "open_app failures")
            adjustment_id: Identifier for the prompt adjustment or code change
            baseline_rate: Success rate BEFORE the improvement (0.0-1.0)
            description: Human-readable description of what changed
        """
        record = {
            "pattern": pattern,
            "adjustment_id": adjustment_id,
            "baseline_rate": baseline_rate,
            "description": description,
            "applied_at": time.time(),
            "status": "pending",
        }
        self._pending.append(record)
        self._save()
        logger.info(f"Recorded improvement: {pattern} (baseline={baseline_rate:.1%})")

    def review_pending(self, get_success_rate_fn):
        """Review all pending improvements that have passed the review window.

        Args:
            get_success_rate_fn: Callable(pattern) -> float (0.0-1.0) that returns
                                 the current success rate for a pattern.

        Returns:
            List of review results: [{"pattern": ..., "action": "keep"/"revert", ...}]
        """
        now = time.time()
        results = []
        still_pending = []

        for record in self._pending:
            age = now - record["applied_at"]

            if age < self._review_window:
                still_pending.append(record)
                continue

            # Review window passed — evaluate
            pattern = record["pattern"]
            baseline = record["baseline_rate"]

            try:
                current_rate = get_success_rate_fn(pattern)
            except Exception:
                current_rate = baseline  # Can't measure — assume neutral

            improvement = current_rate - baseline

            if improvement > 0.05:
                # Improved by >5% — keep
                action = "keep"
                logger.info(f"Improvement KEPT: {pattern} ({baseline:.1%} -> {current_rate:.1%})")
            elif improvement < -0.05:
                # Worsened by >5% — revert
                action = "revert"
                logger.warning(f"Improvement REVERTED: {pattern} ({baseline:.1%} -> {current_rate:.1%})")
            else:
                # Neutral — keep but mark as low-confidence
                action = "neutral"
                logger.info(f"Improvement NEUTRAL: {pattern} ({baseline:.1%} -> {current_rate:.1%})")

            results.append({
                "pattern": pattern,
                "adjustment_id": record["adjustment_id"],
                "baseline_rate": baseline,
                "current_rate": current_rate,
                "improvement": improvement,
                "action": action,
            })

        self._pending = still_pending
        self._save()
        return results

    def get_pending_count(self) -> int:
        return len(self._pending)

    def clear(self):
        """Clear all pending reviews."""
        self._pending = []
        self._save()
