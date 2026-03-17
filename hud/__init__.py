"""
HUD Overlay package — JARVIS visual dashboard.

FastAPI + WebSocket server that bridges the event bus to browser clients.

Usage:
    from hud.server import start_hud_server
    start_hud_server(port=8767)  # non-blocking daemon thread
"""

from hud.server import start_hud_server, HudServer

__all__ = ["start_hud_server", "HudServer"]
