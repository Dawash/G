# Refactor Rules

Mandatory coding rules for the G architecture refactor. Every PR/change must follow these.

---

## 1. File Size Limits

- **Max 500 lines per module** (hard limit 700 for complex modules like brain_service.py)
- If a module exceeds 500 lines, split it before adding more code
- Prefer many small focused files over few large ones

## 2. No Module-Level Mutable Globals

- **Never** define mutable state at module level (`_cache = {}`, `_undo_stack = []`, etc.)
- All mutable state lives inside class instances or dataclasses
- Constants (frozen sets, tuples, compiled regexes) at module level are fine
- Exception: module-level `logger = logging.getLogger(__name__)` is OK

## 3. Single Responsibility

- Every module/class has **one main job** described in its docstring
- If the docstring needs "and" to describe what it does, split it
- A service should not know about other services' internals

## 4. Dependency Injection

- Services receive dependencies via `__init__()` parameters, not global imports
- Use `app/container.py` to wire dependencies at startup
- **Never** use `getattr(some_function, '_hidden_attr')` to pass state between modules
- **Never** monkey-patch functions with runtime attributes

## 5. Event Bus for Cross-Cutting Concerns

- Modules communicate via `core/events.py` event bus, not direct imports
- Examples: reminder fired → event → speech speaks it; tool executed → event → metrics logs it
- Events are fire-and-forget (async); responses go through return values or callbacks

## 6. Dataclasses for Structured State

- Use `@dataclass` for any structured runtime state (ToolResult, SessionState, ReminderEntry, etc.)
- No plain dicts for structured data that crosses module boundaries
- Dataclasses go in `core/state.py` (shared) or in the owning module (local)

## 7. Type Hints

- All public function signatures must have type hints
- Internal/private functions: type hints encouraged but not required
- Use `from __future__ import annotations` for forward references

## 8. Error Handling

- **Never** bare `except:` — always catch specific exceptions
- **Never** silently swallow exceptions — at minimum log them
- Tool handlers return error strings (not raise) for user-facing errors
- Infrastructure errors (disk full, network down) propagate up

## 9. Threading Rules

- All shared mutable state protected by `threading.Lock()`
- Document which lock protects which state
- Prefer `with lock:` context manager over manual acquire/release
- No nested locks (deadlock risk) — if unavoidable, document lock ordering

## 10. Import Rules

- No circular imports — if A imports B, B must not import A
- Use the dependency injection container to break circular dependencies
- Lazy imports (`import X` inside function body) only for optional/heavy dependencies
- Import order: stdlib → third-party → project (with blank lines between groups)

## 11. Testing

- Every new module should be testable in isolation (mock its dependencies)
- Tool handlers must be pure functions where possible (input → output, no side effects)
- Integration tests use the DI container with mock providers

## 12. Platform Isolation

- All Windows-specific code lives under `platform_impl/windows/`
- Other modules must not import `winreg`, `ctypes.windll`, `pygetwindow`, etc. directly
- Platform modules expose clean interfaces that could be swapped for Linux/macOS

## 13. Configuration

- No hardcoded paths, URLs, or magic numbers
- All configurable values go through `core/config_service.py`
- Timeouts, retry counts, cache TTLs, thresholds — all named constants or config

## 14. Logging

- Use `logging.getLogger(__name__)` in every module
- Log levels: DEBUG (internal flow), INFO (user-visible events), WARNING (recoverable issues), ERROR (failures)
- **Never** `print()` in library code — only in CLI entry points
- Include context in log messages: `logger.info("Tool %s executed in %.1fs", tool_name, elapsed)`

## 15. Migration Process

- Migrate one module at a time (see `docs/module-migration-map.md` for order)
- After migrating: old module becomes thin import wrapper (re-exports from new location)
- Wrappers stay until all callers are updated, then delete
- Run `python run.py` after every migration step — never leave system broken
- Run `python self_test.py` as regression check

## 16. Naming Conventions

- Modules: `snake_case.py`
- Classes: `PascalCase` (e.g., `BrainService`, `ToolRegistry`, `AppManager`)
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private: prefix with `_` (single underscore)
- No abbreviations in public APIs (`config` not `cfg`, `message` not `msg`)

## 17. Backward Compatibility During Migration

- Old entry points (`run.py`, `main.py`) keep working throughout migration
- Old module imports keep working via re-export wrappers
- `brain.py` and `assistant.py` are migrated last (they're the integration points)
- Feature modules (weather, news, etc.) can be migrated independently

## 18. Separate Orchestration from Execution

- **Orchestration** modules (orchestration/, llm/) decide *what* to do
- **Execution** modules (platform_impl/, features/) *do the thing*
- Tools executor (tools/executor.py) is the boundary — it routes from orchestration to execution
- Orchestration modules must never call subprocess, pyautogui, or other side-effecting APIs directly
- Execution modules must never hold conversation context or decide which tool to call

## 19. Separate Tool Metadata from Tool Logic

- Tool JSON schemas (names, descriptions, parameter definitions) → `tools/schemas.py`
- Tool name aliases and argument normalization → `tools/schemas.py`
- Actual tool handler implementations → `platform_impl/windows/*` or `features/*/service.py`
- Tool execution dispatch and pre/post hooks → `tools/executor.py`
- Never mix schema definitions with handler code in the same file

## 20. Prefer Deterministic Routing over LLM

- Use regex patterns for unambiguous commands before falling back to LLM classification
- `_QUICK_PATTERNS`, `_DIRECT_TOOL_PATTERNS` → handle 80%+ of requests with zero LLM cost
- LLM classification is a fallback for genuinely ambiguous requests, not the default path
- Exact-match commands ("what time is it", "pause music") should bypass LLM tool-calling entirely
- Rationale: Ollama with 18 tools takes 6.5s; regex takes <1ms

## 21. Risky Actions Must Use Safety Policy

- All tools that execute system commands, modify files, or send data externally must pass through `tools/safety_policy.py`
- Destructive actions (delete, uninstall, shutdown) require voice confirmation via `confirm_with_user()`
- Terminal commands must pass blocklist validation before execution
- File operations must check blocked directories before proceeding
- No new tool handler may call subprocess or modify filesystem without safety check
- `exec()` / `eval()` must never be used with unsanitized LLM output

## 22. Documentation

- Every module has a docstring explaining: what it does, what it replaces, its dependencies
- `docs/module-migration-map.md` stays up-to-date as modules are migrated
- No separate API docs — code is the documentation (docstrings + type hints)
