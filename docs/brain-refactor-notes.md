# Brain Refactor Notes

**Date**: 2026-03-07
**Phase**: 6 — Decompose brain.py into LLM services

## Extracted Responsibilities

| Responsibility | From (brain.py) | To | Lines |
|---|---|---|---|
| Context/history management | `Brain.messages`, `_trim_context()`, `_collapse_completed_turn()`, `_get_clean_messages()`, `_pop_user_message()`, `reset_context()` | `llm/context_manager.py::ContextManager` | 300 |
| Topic tracking | `_TOPIC_KEYWORDS`, `_extract_topic()`, `_update_topic()`, `_current_topic`, `_topic_turn_count` | `llm/context_manager.py::ContextManager` | (included above) |
| Ambient context | `_CONTEXT_TRIGGERS`, `_get_ambient_context()` | `llm/context_manager.py::ContextManager` | (included above) |
| Idle detection | `_last_think_time`, idle reset in `think()` | `llm/context_manager.py::ContextManager.check_idle_reset()` | (included above) |
| Mode classification | `_QUICK_PATTERNS`, `_AGENT_PATTERNS`, `_classify_mode()` | `llm/mode_classifier.py` (Phase 4) | 174 |
| Mode result type | (was a plain string) | `llm/mode_classifier.py::ModeDecision` dataclass | (included above) |
| Response sanitization | `_sanitize_response()` | `llm/response_builder.py::sanitize_response()` | 111 |
| LLM refusal detection | `_is_llm_refusal()` | `llm/response_builder.py::is_llm_refusal()` | (included above) |
| Tool retry suggestion | `_suggest_tool_for_retry()` | `llm/response_builder.py::suggest_tool_for_retry()` | (included above) |
| System prompt building | `_build_brain_system_prompt()`, `_build_prompt_system()` | `llm/prompt_builder.py` (Phase 4) | 138 |
| Safety policy | `_CONFIRM_TOOLS`, `_confirm_with_user()`, `_validate_tool_choice()` | `tools/safety_policy.py` (Phase 4) | 108 |
| Facade/coordinator | (new) | `llm/brain_service.py::BrainService` | 76 |

## Wiring Strategy

Brain creates a `BrainService` in `__init__`, holds a reference to its `ContextManager`:

```python
self._svc = BrainService(username, ainame, max_context=6)
self._ctx = self._svc.ctx
```

`messages` and `max_context` are properties on Brain that delegate to `_ctx`:

```python
@property
def messages(self):
    return self._ctx.messages
```

All context methods in Brain are thin delegates to `_ctx`. All sanitization methods delegate to `response_builder`. Mode classification returns `ModeDecision` with confidence and reason.

## What Remains in brain.py (2168 lines)

| Area | Lines | Why it stays |
|---|---|---|
| Brain class shell + `__init__` | ~80 | Constructor, properties, cognitive init |
| `think()` main entry | ~260 | Complex orchestration: idle check, cognitive resolve, language detect, mode routing, error handling |
| `_think_native()` | ~190 | Native tool-calling loop with JSON extraction fallback |
| `_think_prompt_based()` | ~90 | Prompt-based tool-calling with refusal retry |
| `_call_openai_style()` / `_call_anthropic_style()` | ~170 | Provider-specific API calls |
| `_run_agent_mode()` / `_run_research()` | ~130 | Agent and research mode execution |
| `quick_chat()` / `stream_response()` | ~130 | Lightweight LLM calls |
| `execute_tool()` + `_execute_tool_inner()` | ~430 | Tool dispatch (30+ tool handlers) |
| `_auto_escalate_to_agent()` | ~55 | Post-tool verification escalation |
| Dynamic tool factory | ~90 | `create_tool()`, `execute_dynamic_tool()` |
| Module-level state + helpers | ~100 | Undo stack, action log, cache, learning |
| Undo registry + recent actions | ~50 | `_register_undo()`, `undo_last_action()`, `_record_action()` |
| Warm-up + trace | ~60 | Ollama model loading, brain_trace.json |

## Still-Coupled Areas

1. **`execute_tool._last_user_input`** — tool executor reads this function attribute set by `think()`. Should become explicit parameter passing.
2. **`_brain_state` module-level singleton** — undo stack, recent actions, response cache, action log, dynamic tools all live on this. Should be injected via container.
3. **`_experience_learner` / `_log_learning._cognition`** — function attributes used for cognitive integration. Should be proper dependency injection.
4. **`_last_created_file` module global** — pronoun resolution ("open it") tracks this. Should be a field on `_brain_state`.
5. **Tool dispatch giant if/elif** — `_execute_tool_inner()` is 320 lines of tool routing. Should be a registry pattern.
6. **API call duplication** — `_call_openai_style()`, `_call_anthropic_style()`, and `quick_chat()` all construct HTTP requests independently. Should share a common provider invocation layer.
7. **Cognitive integration in `think()`** — pronoun resolution, decomposition, self-analysis scattered through `think()` and `_update_topic()`. Should be a single cognitive middleware.

## Next Recommended Extractions (priority order)

### P1 — Tool execution (biggest impact, ~430 lines)
Extract `execute_tool()` + `_execute_tool_inner()` + `_auto_escalate_to_agent()` into `tools/executor.py` with a registry-based dispatch pattern. This is the single largest block of code in brain.py and has the least coupling to Brain's internal state.

### P2 — Provider invocation (~170 lines)
Extract `_call_openai_style()`, `_call_anthropic_style()`, `_call_llm_native()`, `_call_llm_simple()` into `llm/provider_invoker.py`. Unify the HTTP request construction. `quick_chat()` and `stream_response()` can delegate to the same invoker.

### P3 — Tool-calling orchestration (~280 lines)
Extract `_think_native()` and `_think_prompt_based()` into `llm/tool_caller.py`. These use context_manager, response_builder, and the provider invoker, so they should be extracted after P2.

### P4 — Agent/research modes (~130 lines)
Extract `_run_agent_mode()` and `_run_research()` into their own modules. Agent mode is already mostly in `desktop_agent.py` — the Brain wrapper just adds mic monitoring.

### P5 — Dynamic tool factory (~90 lines)
Move `create_tool()`, `execute_dynamic_tool()`, `_save_custom_tools()`, `_load_custom_tools()` into `tools/dynamic_tools.py`.

## Module Structure After This Phase

```
llm/
  __init__.py              — package marker
  brain_service.py         — facade: coordinates context + mode + response
  context_manager.py       — messages, trimming, collapsing, topic, ambient
  mode_classifier.py       — quick/agent/research classification + ModeDecision
  response_builder.py      — sanitize, refusal detection, tool suggestion
  prompt_builder.py        — system prompt construction (Phase 4)
  planner.py               — (stub) future: task planning
  provider_registry.py     — (stub) future: provider factory

tools/
  safety_policy.py         — confirmation, tool validation (Phase 4)

brain.py                   — Brain class: think(), tool execution, API calls
brain_defs.py              — tool definitions, terminal/file/software handlers
```
