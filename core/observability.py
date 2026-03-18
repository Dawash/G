"""Observability — centralized metrics, error tracking, and health monitoring.

Subscribes to the event bus and automatically tracks:
- Success/failure rates per component
- Latency percentiles (p50, p95)
- Error frequency and patterns
- System health score

Usage:
    from core.observability import metrics, start_observability
    start_observability()  # once at startup
    metrics.record_success("brain.think", duration_ms=2500)
    metrics.get_dashboard()  # full status for HUD
"""

import time
import threading
import logging
from typing import Dict, List, Optional
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ComponentMetrics:
    """Metrics for a single component."""
    name: str
    success_count: int = 0
    failure_count: int = 0
    total_duration_ms: float = 0
    recent_latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=20))
    last_success_time: float = 0
    last_failure_time: float = 0
    consecutive_failures: int = 0

    @property
    def total_calls(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_count / self.total_calls

    @property
    def avg_latency_ms(self) -> float:
        if not self.recent_latencies:
            return 0
        return sum(self.recent_latencies) / len(self.recent_latencies)

    @property
    def p95_latency_ms(self) -> float:
        if not self.recent_latencies:
            return 0
        s = sorted(self.recent_latencies)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def health(self) -> str:
        if self.consecutive_failures >= 5:
            return "critical"
        if self.consecutive_failures >= 3 or (self.total_calls > 5 and self.success_rate < 0.7):
            return "degraded"
        return "healthy"

    def record_success(self, duration_ms: float = 0):
        self.success_count += 1
        self.consecutive_failures = 0
        self.last_success_time = time.time()
        if duration_ms > 0:
            self.recent_latencies.append(duration_ms)
            self.total_duration_ms += duration_ms

    def record_failure(self, error: str = "", duration_ms: float = 0):
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if duration_ms > 0:
            self.recent_latencies.append(duration_ms)
        self.recent_errors.append({"error": error[:200], "time": time.time()})

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate * 100, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "health": self.health,
            "last_error": self.recent_errors[-1]["error"] if self.recent_errors else "",
        }


class MetricsCollector:
    """Centralized metrics collection across all components."""

    def __init__(self):
        self._components: Dict[str, ComponentMetrics] = {}
        self._global_errors: deque = deque(maxlen=100)
        self._lock = threading.RLock()
        self._running = False
        self._start_time = time.time()
        self._interaction_count = 0

    def _get_component(self, name: str) -> ComponentMetrics:
        if name not in self._components:
            self._components[name] = ComponentMetrics(name=name)
        return self._components[name]

    def record_success(self, component: str, duration_ms: float = 0,
                       metadata: Optional[dict] = None):
        with self._lock:
            self._get_component(component).record_success(duration_ms)

    def record_failure(self, component: str, error: str = "",
                       duration_ms: float = 0, metadata: Optional[dict] = None):
        with self._lock:
            comp = self._get_component(component)
            comp.record_failure(error, duration_ms)
            self._global_errors.append({
                "component": component, "error": error[:200],
                "time": time.time(), "metadata": metadata or {},
            })
            if comp.consecutive_failures >= 3:
                try:
                    from core.event_bus import bus
                    bus.publish("system.error_alert", {
                        "component": component,
                        "consecutive_failures": comp.consecutive_failures,
                        "last_error": error[:200],
                        "health": comp.health,
                    })
                except Exception:
                    pass

    def record_interaction(self):
        self._interaction_count += 1

    def get_component(self, name: str) -> dict:
        with self._lock:
            if name in self._components:
                return self._components[name].to_dict()
            return {"name": name, "health": "unknown", "success_rate": 100}

    def get_errors(self, n: int = 10) -> List[dict]:
        with self._lock:
            return list(self._global_errors)[-n:]

    def get_health(self) -> str:
        with self._lock:
            if not self._components:
                return "healthy"
            healths = [c.health for c in self._components.values() if c.total_calls > 0]
            if not healths:
                return "healthy"
            if any(h == "critical" for h in healths):
                return "critical"
            if sum(1 for h in healths if h == "degraded") >= 2:
                return "degraded"
            return "healthy"

    def get_dashboard(self) -> dict:
        with self._lock:
            uptime = time.time() - self._start_time
            h, m = int(uptime // 3600), int((uptime % 3600) // 60)
            components = {
                n: c.to_dict() for n, c in self._components.items() if c.total_calls > 0
            }
            return {
                "health": self.get_health(),
                "uptime": f"{h}h {m}m",
                "uptime_seconds": round(uptime),
                "interactions": self._interaction_count,
                "components": components,
                "recent_errors": list(self._global_errors)[-5:],
                "total_errors": sum(c.failure_count for c in self._components.values()),
                "total_successes": sum(c.success_count for c in self._components.values()),
            }

    def start_health_publisher(self):
        if self._running:
            return
        self._running = True

        def _publisher():
            while self._running:
                try:
                    from core.event_bus import bus
                    bus.publish("system.health_update", self.get_dashboard())
                except Exception:
                    pass
                time.sleep(30)

        threading.Thread(target=_publisher, daemon=True, name="metrics-publisher").start()

    def stop(self):
        self._running = False


metrics = MetricsCollector()


def start_observability():
    """Subscribe to bus events and start collecting metrics. Call once at startup."""
    try:
        from core.event_bus import bus

        @bus.on("system.error_alert")
        def _on_error_alert(event):
            data = getattr(event, "data", getattr(event, "payload", {}))
            logger.warning(
                "Component %s: %d consecutive failures — %s",
                data.get("component", "?"),
                data.get("consecutive_failures", 0),
                data.get("last_error", ""),
            )

        metrics.start_health_publisher()
        logger.info("Observability started")
    except Exception as e:
        logger.debug(f"Observability start failed: {e}")
