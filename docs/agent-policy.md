# Desktop Agent Policy

Rules governing when and how the desktop agent operates.

## Escalation Policy

The desktop agent is NOT a default fallback. It activates only through explicit escalation:

```
User speaks -> STT -> text
  |
  Layer 1: Meta-commands                    [0ms]
  |
  Layer 2a: Fast path (deterministic)       [<50ms]
  |
  Layer 2b: Brain/LLM (quick tool call)     [500-3000ms]
  |
  Layer 2c: Agent mode (explicit only)      [5-120s]
```

### When Agent Mode Activates

| Trigger | Example | Who decides |
|---------|---------|-------------|
| `agent_task` tool call | Brain calls `agent_task(goal)` | LLM |
| `_AGENT_PATTERNS` match | "fill out the form", "snap Chrome left" | brain.py regex |
| Verification failure + escalation | open_app fails -> agent tries desktop automation | tools/executor.py |
| User says "use agent mode" | Explicit request | User |

### When Agent Mode Does NOT Activate

- Simple tool calls (open app, search, weather, time)
- Questions and conversation
- Fast-path commands (deterministic routing)
- Terminal/file/software operations (run_terminal, manage_files)

## Execution Budget

Every agent execution has hard limits:

| Budget | Default | Purpose |
|--------|---------|---------|
| `max_steps` | 12 | Maximum observe-think-act cycles |
| `max_retries` | 2 | Max retries per failed step |
| `max_wall_clock` | 120s | Total wall-clock time limit |
| `max_recon_turns` | 3 | Max turns for reconnaissance phase |
| `max_backtrack` | 2 | Max plan replans before giving up |
| `checkpoint_interval` | 3 | Actions between state checkpoints |

Callers can override defaults:
```python
agent = DesktopAgentV2(registry, budget={
    "max_steps": 8,
    "max_wall_clock": 60,
})
```

## 3-Phase Architecture

### Phase 1: RECON (max 30s)
- Observe screen without acting
- Detect blockers (popups, dialogs, login screens)
- Auto-dismiss simple blockers (cookies, profile pickers)
- Pause for user on sensitive screens (login, payment, CAPTCHA, 2FA)

### Phase 2: EXECUTE (main loop)
- Plan-guided: LLM generates steps, agent follows plan
- Direct tool shortcuts: skip LLM for obvious steps ("open Chrome" -> open_app)
- Observe -> Think -> Act -> Verify per step
- Checkpoint every 3 actions for crash recovery
- Backtrack on failure: replan remaining steps with different approach

### Phase 3: VERIFY
- Check goal completion from tool results (not just vision)
- Self-heal: learn from failure patterns for future sessions

## Step Verification (Multi-Layer)

Priority order (fast to slow):
1. **Tool result keywords** — "opened", "typed", "searched" (instant)
2. **Window title check** — OS-level, reliable (fast)
3. **File existence** — for create_file steps (fast)
4. **Process check** — window list match (fast)
5. **Web extraction** — browser URL + content (moderate)
6. **Negative check** — error indicators override success (instant)
7. **Vision fallback** — llava screenshot analysis (slow, 2-5s)

## Failure Recovery

### Escalation Levels
1. **L1: LLM Diagnosis** — Ask LLM what went wrong, get specific fix action
2. **L2: Alternative Tool** — Try tool from escalation map (e.g., open_app fails -> search_in_app)
3. **L3: Backtrack/Replan** — Generate entirely new plan for remaining steps
4. **L4: Give Up** — Report partial progress after max stuck count (3)

### Tool Alternatives (Escalation Map)
```
open_app        -> search_in_app, run_command
click_at        -> search_in_app, press_key
search_in_app   -> google_search, type_text
focus_window    -> open_app, click_at
toggle_setting  -> run_terminal, run_command
type_text       -> press_key
run_command     -> run_terminal
manage_software -> run_terminal
manage_files    -> run_terminal
```

### Self-Healing
After each failed session, the agent:
- Analyzes tool memory for high-failure tools
- Scans assistant.log for recurring error patterns
- Generates recovery hints for future sessions
- Persists hints to `learned_hints.json`

## Safety Rules

### Takeover Screens (Agent Pauses)
- Login / password screens
- Payment / checkout
- CAPTCHA / human verification
- Two-factor authentication
- UAC / administrator prompts

Agent speaks a message and waits for user to say "continue" or "stop".
Timeout: 120 seconds.

### Pre-Action Safety
- Block same tool failing 2+ times with same args
- Validate tool names against known set (fuzzy match on typos)
- Per-tool timeouts (3s for click, 120s for software install)

### Silent Execution
- Agent thinks in console only (print statements)
- Speaks only final result and progress announcements
- Sub-agents don't speak at all

## Structured Step Traces

Every step produces a trace entry:
```json
{
  "turn": 3,
  "saw": "Active window: Chrome - Google",
  "decided": "USE_TOOL: search for Python tutorials",
  "tool": "search_in_app",
  "args": {"app": "Chrome", "query": "Python tutorials"},
  "result": "Searching for Python tutorials in Chrome",
  "parsed_status": "success",
  "next_hint": "",
  "plan_step": "Search for Python tutorials in the browser"
}
```

Access via `agent.get_step_traces()` after execution.

## Module Responsibilities

| Module | Class | Lines | Role |
|--------|-------|-------|------|
| `planner.py` | `AgentPlanner` | ~250 | Plan generation, replanning, direct tool shortcuts |
| `observer.py` | `ScreenObserver` | ~280 | Screenshots, vision, window/process enumeration, browser content |
| `verifier.py` | `StepVerifier` | ~230 | Step verification (7 layers), goal completion, result parsing |
| `recovery.py` | `FailureRecovery` | ~250 | Diagnosis, tool memory, stuck detection, self-healing |
| `desktop_agent.py` | `DesktopAgentV2` | ~700 | Orchestrator: phases, main loop, think, act, safety, helpers |

## Migration Path

```python
# Before (current):
from desktop_agent import DesktopAgent

# After (same API):
from automation.desktop_agent import DesktopAgentV2 as DesktopAgent
```

The original `desktop_agent.py` remains fully functional. Switch callers
one at a time: brain.py -> assistant.py -> tests.
