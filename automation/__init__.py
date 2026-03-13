"""
Desktop automation subsystem — agentic mode + UI Automation.

Agent modules:
  - desktop_agent.py: Top-level agent orchestrator (DesktopAgentV2)

UI Automation modules (Phase 16):
  - ui_control.py: UIA-based control interaction (find/click/set_text/inspect)
  - window_manager.py: Window management (list/snap/arrange)
  - resolve.py: Tiered target resolution (UIA → keyboard → vision)

Browser automation (Phase 17):
  - browser_driver.py: Chrome DevTools Protocol + UIA + keyboard fallback
  - cdp_session.py: Persistent CDP WebSocket session (CDPSession singleton)

App drivers (Phase 20):
  - drivers/base.py: Base driver class with action registry
  - drivers/browser.py: Chrome/Edge/Firefox operations
  - drivers/explorer.py: File Explorer operations
  - drivers/settings.py: Windows Settings operations
"""
