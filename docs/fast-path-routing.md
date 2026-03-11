# Fast-Path Routing

Deterministic fast-path router for high-frequency daily commands.
Skips the LLM entirely for obvious single-tool requests.

## Where It Fits

```
User speaks -> STT -> text
  |
  Layer 1: Meta-commands (skip, undo, repeat, exit, connect)     [0ms, no API]
  |
  Layer 2a: FAST PATH  <-- NEW                                   [<50ms, no API]
  |
  Layer 2b: Brain/LLM (think -> tool calling -> response)        [500-3000ms]
  |
  Layer 3: Keyword fallback (when Brain unavailable)             [0ms, no API]
```

## Commands Using Fast Path

| Command Pattern | Handler | Example | Response |
|----------------|---------|---------|----------|
| `open <app>` | open_app | "open Chrome" | "Opening Chrome." |
| `close <app>` | close_app | "close Notepad" | "Closing Notepad." |
| `minimize <app>` | minimize_app | "minimize Discord" | "Minimizing Discord." |
| `what's the time` | time | "what time is it" | "It's Monday, 3:00 PM." |
| `weather` | weather | "what's the weather" | Full weather report |
| `forecast` | forecast | "will it rain" | Full forecast |
| `remind me to X at Y` | set_reminder | "remind me to call at 5pm" | "Reminder set for 5:00 PM." |
| `list reminders` | list_reminders | "show my reminders" | Reminder list |
| `play <query>` | play_music | "play jazz" | Music playback result |
| `pause music` | pause_music | "pause the music" | "Paused." |
| `next / skip` | next_track | "next song" | "Next track." |
| `search for <query>` | google_search | "search for Python" | "Searching for Python." |

## Commands That Still Use the LLM

These always go through Brain/LLM (Layer 2b):

| Type | Example | Why |
|------|---------|-----|
| Multi-step | "open Chrome and then search for weather" | Contains "and then" |
| Questions | "how do I open Chrome" | Contains "how" |
| Comparisons | "compare Chrome and Firefox" | Contains "compare" |
| Explanations | "explain the weather forecast" | Contains "explain" |
| Conversational | "tell me about Chrome features" | Contains "tell me about" |
| Pronoun references | "open it" | Needs context from Brain |
| Complex tool use | "create a calculator in HTML" | No fast-path pattern |
| Agent tasks | "fill out the form on that page" | Screen interaction needed |
| Research queries | "what are the best laptops for coding" | Multi-source research |
| Ambiguous | "Chrome" (open? close? search?) | No verb = ambiguous |

## Ambiguity Fallback Rules

Fast path rejects and falls through to Brain when:

1. **Complexity guards trigger** — input contains connectors ("and then", "after that"), conditionals ("if", "when", "unless"), or question words ("how", "why", "explain", "compare", "should I").

2. **Pronoun reference** — "open it", "close that" need Brain's pronoun resolution (last created file, last opened app).

3. **App not found** — `open_app` returns "not found" → falls through so Brain can suggest alternatives.

4. **No pattern match** — anything that doesn't match a fast-path regex goes to Brain.

5. **Handler unavailable** — if the action_registry doesn't have the handler, falls through.

## Confidence Model

| Level | Routing | Example |
|-------|---------|---------|
| **High** (deterministic) | Fast path → direct execution | "open Chrome", "what's the time" |
| **Medium** (quick tool) | Brain → quick mode → single tool call | "create a simple calculator" |
| **Low** (ambiguous) | Brain → LLM classification → tool/agent | "help me with this spreadsheet" |
| **Complex** (multi-step) | Brain → agent mode → plan+execute | "fill out the form and submit" |

## Performance Impact

- **Fast path**: <50ms (regex match + direct handler call)
- **Brain quick mode**: 500-2000ms (LLM round-trip + tool call)
- **Brain agent mode**: 5-30s (multi-step plan + execute + verify)

For the ~40% of commands that are simple open/close/time/weather/search, this eliminates the LLM round-trip entirely.
