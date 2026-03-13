# agents/ — Multi-Agent "G Swarm" System
#
# Architecture: 5 specialized agents communicating via shared Blackboard.
#
#   blackboard.py     — Shared state (dict + SQLite + vector memory)
#   base.py           — BaseAgent interface
#   planner.py        — Tree-of-Thoughts hierarchical planner
#   executor.py       — Desktop execution (wraps desktop_agent.py)
#   critic.py         — Verification + scoring after actions
#   researcher.py     — Web research on failure/stuck
#   memory_agent.py   — Skill evolution + reflexion learning
#   orchestrator.py   — State machine that routes between agents
#
# Entry point:
#   from agents.orchestrator import SwarmOrchestrator
#   result = SwarmOrchestrator(brain).execute(goal)
