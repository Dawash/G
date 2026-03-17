# core — Shared infrastructure: events, state, logging, metrics, config.
from core.event_bus import bus, Event          # noqa: F401 — re-exported for convenience
from core.topics import Topics                 # noqa: F401
