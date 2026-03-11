# Memory & Workflows

## Memory Model

G has four distinct memory concepts:

| Layer | Storage | Lifetime | Purpose |
|-------|---------|----------|---------|
| **Session events** | SQLite `events` table | Per-session | Track what happened this session (commands, errors, tool calls) |
| **Persistent facts** | SQLite `memories` table | Permanent | User-told facts ("my favorite color is blue", "my dog's name is Max") |
| **Preferences** | SQLite `memories` (category=preferences) | Permanent | Response style, confirmation level, speaking style, news categories |
| **Habits** | SQLite `usage_log` table | Permanent | Temporal usage patterns for proactive suggestions |

### Key Classes

- `MemoryStore` (`memory.py`) — SQLite-backed store for facts, events, and usage logging
- `UserPreferences` (`memory.py`) — Preference management with defaults, backed by MemoryStore
- `HabitTracker` (`memory.py`) — Analyzes usage_log for temporal patterns

### Private Mode

When private mode is enabled (`memory_control` tool, action=`private_on`):
- `MemoryStore.log_event()` silently skips logging
- `MemoryStore.log_usage()` silently skips logging
- Existing memories remain accessible
- New `remember` commands still work (explicit user intent)
- Disabled with `private_off`

## Memory Controls

The `memory_control` tool gives users direct control over G's memory. Registered in `tools/memory_workflow_tools.py`, logic in `features/memory/controls.py`.

### Actions

| Action | Example | What it does |
|--------|---------|-------------|
| `remember` | "remember my favorite color is blue" | Stores a fact (parses "X is Y", "key: value" formats) |
| `forget` | "forget my favorite color" | Removes a specific fact by key. "forget everything" clears all. |
| `recall` | "what do you remember about me" | Lists all stored facts, grouped by category |
| `search` | "do you remember anything about dogs" | Keyword search across all memories |
| `private_on` | "enable private mode" | Stops event/usage logging |
| `private_off` | "disable private mode" | Resumes logging |
| `preferences` | "show my preferences" / "set response_style to concise" | View or set user preferences |

### Fact Parsing

The `remember` action parses natural input:
- `"key: value"` → stored as key/value
- `"X is Y"` / `"X are Y"` → stored as X/Y
- Plain text → stored with auto-generated key

## Preference System

Preferences affect G's behavior. Stored via `UserPreferences` class.

### Default Values

| Key | Default | Options |
|-----|---------|---------|
| `response_style` | `normal` | `concise`, `normal`, `detailed` |
| `confirmation_level` | `normal` | `strict`, `normal`, `relaxed` |
| `speaking_style` | `casual` | `formal`, `casual`, `playful` |
| `preferred_news` | `general` | Any news category |

### System Prompt Integration

When `response_style` is set:
- `concise` → system prompt adds "User prefers CONCISE responses"
- `detailed` → system prompt adds "User prefers DETAILED responses"

Wired through `Brain.user_preferences` → `_get_pref_dict()` → `build_brain_system_prompt()`.

## Workflow System

Simple named sequences of tool calls. Registered in `tools/memory_workflow_tools.py`, logic in `features/workflows/`.

### Structure

```
features/workflows/
  registry.py    — WorkflowRegistry: stores and retrieves workflows
  executor.py    — execute_workflow(): runs a workflow step by step
```

### Built-in Workflows

| Name | Steps |
|------|-------|
| `start my workday` | get_weather → get_news → list_reminders |
| `meeting mode` | minimize_app(all) → open_app(Teams/Zoom) |
| `coding setup` | open_app(VS Code) → open_app(Terminal) → run_terminal(git status) |
| `end my day` | list_reminders → get_weather(forecast) |

### Workflow Actions

The `run_workflow` tool supports:

| Action | What it does |
|--------|-------------|
| `run` | Execute a workflow by name |
| `list` | Show all available workflows |
| `create` | Create a new workflow with steps |
| `delete` | Delete a user-created workflow (built-ins can't be deleted) |

### Creating Workflows

Via the `run_workflow` tool with action=`create`:
```json
{
  "name": "morning routine",
  "action": "create",
  "description": "My morning routine",
  "steps": [
    {"tool": "get_weather", "args": {}},
    {"tool": "play_music", "args": {"query": "morning playlist"}}
  ]
}
```

User-created workflows persist in `workflows.json`.

### Execution

`execute_workflow()` iterates steps sequentially, calling `ToolExecutor.execute()` for each. Results are collected and returned as a summary. If a step fails, execution continues with remaining steps.

## File Map

| File | Role |
|------|------|
| `memory.py` | MemoryStore, UserPreferences, HabitTracker |
| `features/memory/controls.py` | handle_memory_command() — 7 actions |
| `features/workflows/registry.py` | WorkflowRegistry — built-in + user workflows |
| `features/workflows/executor.py` | execute_workflow() — sequential step runner |
| `tools/memory_workflow_tools.py` | Tool registration + handlers for memory_control and run_workflow |
| `brain_defs.py` | LLM tool schemas, core tool list, aliases |
| `llm/prompt_builder.py` | System prompt with preference-aware context |
