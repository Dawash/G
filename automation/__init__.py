"""
Desktop automation subsystem — agentic mode + UI Automation.

Agent modules (Phase 10 decomposition):
  - desktop_agent.py: Top-level agent orchestrator (DesktopAgentV2)
  - planner.py: LLM-driven step planning (AgentPlanner)
  - observer.py: Screenshot + vision + window state (ScreenObserver)
  - verifier.py: Step and goal verification (StepVerifier)
  - recovery.py: Failure diagnosis and retry logic (FailureRecovery)

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

World state (Phase 20):
  - world_state.py: Task-level state tracking, failure taxonomy

The original desktop_agent.py in the project root remains the active
implementation. This package provides the decomposed architecture for
incremental migration.
"""
