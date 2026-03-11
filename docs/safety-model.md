# Safety Model

Formal safety and permissions layer for tool execution in G.

## Safety Levels

Every tool is classified into one of 4 safety levels:

| Level | Confirmation | Audit | Dry-Run | Description |
|-------|-------------|-------|---------|-------------|
| **safe** | Never | Yes | No | Read-only, no side effects |
| **moderate** | Never (configurable) | Yes | No | Mild side effects, easily reversible |
| **sensitive** | Required (conditional) | Yes | Yes | Significant side effects, may need confirmation |
| **critical** | Always required | Yes | Yes | Hard to reverse, system-affecting |

## Tool Classifications

### Safe (no confirmation)

| Tool | Rationale |
|------|-----------|
| `get_weather` | Read-only API call |
| `get_forecast` | Read-only API call |
| `get_time` | Read-only, local |
| `get_news` | Read-only RSS fetch |
| `list_reminders` | Read-only data access |
| `take_screenshot` | Read-only screen capture |
| `find_on_screen` | Read-only UI inspection |
| `web_read` | Read-only page fetch |
| `web_search_answer` | Read-only search |

### Moderate (no confirmation by default)

| Tool | Rationale |
|------|-----------|
| `open_app` | Opens app, easily closed (has undo) |
| `close_app` | Closes app, can reopen |
| `minimize_app` | Reversible window state |
| `google_search` | Opens browser tab |
| `set_reminder` | Creates reminder, can be listed/removed |
| `toggle_setting` | Reversible (has undo) |
| `play_music` | Media playback, can pause |
| `search_in_app` | Opens app + searches |
| `type_text` | Keyboard input, can undo |
| `press_key` | Single keypress |
| `click_at` | Single mouse click |
| `scroll` | Mouse scroll |
| `click_element` | Accessibility tree click |
| `manage_tabs` | Browser tab management |
| `fill_form` | Form field input |
| `create_file` | Creates file (doesn't overwrite) |

### Sensitive (conditional confirmation)

| Tool | Confirms When | Rationale |
|------|--------------|-----------|
| `send_email` | Always | Sends external communication |
| `run_terminal` | Risky commands only | Arbitrary command execution |
| `manage_files` | Delete action only | File deletion is destructive |
| `manage_software` | Install/uninstall only | System package changes |
| `run_self_test` | Always | Runs diagnostic suite |

### Critical (always confirms)

| Tool | Rationale |
|------|-----------|
| `system_command` | Shutdown/restart only (other cmds skip) |
| `restart_assistant` | Restarts the assistant process |
| `agent_task` | Autonomous multi-step desktop automation |

## Confirmation Flow

```
Tool called
  |
  Check safety level
  |
  safe/moderate -> execute immediately
  |
  sensitive/critical -> needs_confirmation(tool, args)
    |
    returns None -> no confirmation needed (e.g. manage_files list)
    |
    returns description -> ask user
      |
      Voice mode:
        speak("Should I {description}? Say yes or no.")
        listen() -> yes/no
        |
        confirmed -> execute
        denied -> return "Cancelled"
      |
      Text mode:
        auto-confirm (log as "auto_confirmed")
      |
      No speak_fn:
        auto-confirm (log as "auto_confirmed")
```

Confirmation status values in the audit log:
- `not_required` — safety level didn't require confirmation
- `confirmed` — user said yes
- `denied` — user said no (tool not executed)
- `auto_confirmed` — skipped confirmation (text mode or no speech)

## Dry-Run Support

Tools that support dry-run mode return a preview of what they would do
without actually executing. Useful for testing and for showing the user
what will happen before confirming.

### Supported Tools

| Tool | Dry-Run Preview |
|------|----------------|
| `run_terminal` | `[DRY-RUN] PowerShell: {command}` or `[DRY-RUN] BLOCKED: {reason}` |
| `manage_files` | `[DRY-RUN] DELETE file.txt` / `[DRY-RUN] MOVE a -> b` |
| `manage_software` | `[DRY-RUN] INSTALL software: VLC (via winget)` |
| `system_command` | `[DRY-RUN] System command: shutdown` |
| `send_email` | `[DRY-RUN] Send email to a@b.com, subject: hello` |

### Usage

```python
from tools.safety_policy import supports_dry_run, dry_run

if supports_dry_run("run_terminal"):
    preview = dry_run("run_terminal", {"command": "Get-PSDrive C"})
    # "[DRY-RUN] PowerShell: Get-PSDrive C"
```

Via the executor:
```python
result = executor.execute("run_terminal", {"command": "ipconfig"},
                          action_registry=registry,
                          dry_run_mode=True)
```

## Terminal Command Safety

Terminal commands (`run_terminal`) have an additional safety layer:

### Blocked Commands (always rejected)

```
format-volume, format c:, format d:
remove-item -recurse -force c:, remove-item -recurse -force /
del /s /q c:, rd /s /q c:, rm -rf /, rm -rf c:
reg delete, reg add, bcdedit, diskpart
net user, net localgroup
set-executionpolicy unrestricted
invoke-webrequest, invoke-restmethod, start-bitstransfer
new-psdrive
```

### Risky Commands (need confirmation)

These patterns trigger confirmation even though `run_terminal` is already
at the sensitive level:

```
remove-item ... -recurse
del ... /path
rd ... /path
stop-process ... -force
restart-service, stop-service, set-service
netsh ... firewall
```

### Safe Commands (no confirmation)

Read-only commands like `Get-PSDrive C`, `ipconfig`, `tasklist`,
`Get-Process`, etc. execute without confirmation.

## Audit Log

Every tool execution is logged to `audit_log.jsonl` (JSON Lines format,
one entry per line, append-only, auto-rotates at 10MB).

### Schema

```json
{
  "timestamp": "2026-03-07T14:30:00.000000+00:00",
  "tool": "run_terminal",
  "arguments": {"command": "Get-PSDrive C"},
  "safety_level": "sensitive",
  "confirmation": "not_required",
  "dry_run": false,
  "success": true,
  "result": "Name  Used (GB)  Free (GB)...",
  "user_utterance": "how much disk space do I have",
  "mode": "quick",
  "duration_ms": 450,
  "verification": null,
  "error": null
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 UTC timestamp |
| `tool` | string | Tool name |
| `arguments` | object | Tool arguments (sensitive values redacted) |
| `safety_level` | string | safe / moderate / sensitive / critical |
| `confirmation` | string | not_required / confirmed / denied / auto_confirmed |
| `dry_run` | bool | Whether this was a dry-run |
| `success` | bool | Whether the tool succeeded |
| `result` | string | Tool result (truncated to 500 chars) |
| `user_utterance` | string | Original user speech (truncated to 200 chars) |
| `mode` | string | Routing mode (quick, agent, research, fast_path) |
| `duration_ms` | int | Execution time in milliseconds |
| `verification` | object? | Verification result if applicable |
| `error` | string? | Error message if failed |

### Sensitive Value Redaction

Arguments containing these key patterns are automatically redacted:
`password`, `token`, `key`, `secret`, `credential`, `body`

Example: `{"to": "a@b.com", "body": "***REDACTED***"}`

### Reading the Log

```python
from tools.audit_log import read_recent

# Last 50 entries
entries = read_recent(50)
for e in entries:
    print(f"[{e['safety_level']}] {e['tool']} -> {e['success']}")
```

## Integration Points

### ToolExecutor (tools/executor.py)

The executor runs safety checks before calling handlers:

```
1. Resolve safety level (max of ToolSpec.safety and global classification)
2. Terminal blocklist check (for run_terminal)
3. Dry-run mode (if requested)
4. Confirmation flow (for sensitive/critical)
5. Cache check
6. Execute handler
7. Post-execution: cache, undo, action log, audit log, learning
```

### Legacy Path (brain.py)

Non-registry tools still go through brain.py's legacy `_CONFIRM_TOOLS` dict,
which is aliased from `tools.safety_policy.CONFIRM_TOOLS`. The audit log
is written for both registry and legacy tool calls via the executor.

### Fast Path (orchestration/fast_path.py)

Fast-path commands skip the safety layer entirely since they only handle
safe commands (open app, time, weather, etc.) that are already classified
as safe or moderate.
