"""
Test harness — feeds commands to Brain.think() programmatically.
Bypasses speech I/O to test tool execution pipeline directly.

Usage: python test_harness.py
"""

import os
import sys
import time
import json
import logging
import traceback

# Ensure project root
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Suppress noisy logs (set to DEBUG for full trace)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Quiet down chatty modules
for mod in ("urllib3", "requests", "faster_whisper", "torch", "PIL"):
    logging.getLogger(mod).setLevel(logging.WARNING)


def load_brain():
    """Initialize Brain with config — same as orchestration/assistant_loop.py does."""
    from config import load_config
    from reminders import ReminderManager
    from memory import MemoryStore, UserPreferences
    from ai_providers import create_provider

    config = load_config()
    provider_name = config.get("provider", "ollama")
    api_key = config.get("api_key", "")
    username = config.get("username", "dawa")
    ainame = config.get("ai_name", "G")
    ollama_model = config.get("ollama_model", "qwen2.5:7b")
    ollama_url = config.get("ollama_url", "http://localhost:11434")

    reminder_mgr = ReminderManager()
    memory = MemoryStore()
    preferences = UserPreferences(memory)

    # Build action_map (same as assistant_loop)
    provider = create_provider(provider_name, api_key,
                               f"You are {ainame}, a helpful AI assistant for {username}.",
                               ollama_url=ollama_url)
    from orchestration.fallback_router import build_action_map
    action_map = build_action_map(reminder_mgr, provider, memory, config)

    from brain import Brain
    brain = Brain(
        provider_name=provider_name,
        api_key=api_key,
        username=username,
        ainame=ainame,
        action_registry=action_map,
        reminder_mgr=reminder_mgr,
        ollama_model=ollama_model,
        user_preferences=preferences,
        ollama_url=ollama_url,
    )
    # Set speak_fn to print (we don't have TTS in test mode)
    brain.speak_fn = lambda text, **kw: print(f"  [TTS] {text}")

    return brain


def run_test(brain, command, test_num, total):
    """Run a single command through brain.think() and report results."""
    print(f"\n{'='*70}")
    print(f"  TEST {test_num}/{total}: {command}")
    print(f"{'='*70}")

    t0 = time.time()
    try:
        result = brain.think(command)
        elapsed = time.time() - t0

        if result is None:
            print(f"  RESULT: None (brain returned None — would fall back to keyword mode)")
            status = "FALLBACK"
        elif "error" in str(result).lower() or "failed" in str(result).lower():
            print(f"  RESULT: {str(result)[:500]}")
            status = "ERROR"
        else:
            print(f"  RESULT: {str(result)[:500]}")
            status = "OK"

        print(f"  TIME: {elapsed:.1f}s")

        # Show trace if available
        if brain.last_call_trace:
            trace = brain.last_call_trace
            tools_used = trace.get("tool_calls", [])
            if tools_used:
                print(f"  TOOLS USED: {[t.get('tool','?') if isinstance(t,dict) else t for t in tools_used]}")
            if trace.get("errors"):
                print(f"  ERRORS: {trace['errors']}")

        return {"command": command, "status": status, "result": str(result)[:200],
                "elapsed": round(elapsed, 1), "tools": len(brain.last_call_trace.get("tool_calls", []) if brain.last_call_trace else [])}

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  EXCEPTION: {e}")
        traceback.print_exc()
        return {"command": command, "status": "EXCEPTION", "result": str(e)[:200],
                "elapsed": round(elapsed, 1), "tools": 0}


def main():
    print("=" * 70)
    print("  G_v0 TEST HARNESS — Programmatic Brain Testing")
    print("=" * 70)

    # Test commands — ordered for logical flow
    # Each tuple: (command, wait_after_seconds)
    tests = [
        # --- Basic system commands ---
        ("what time is it", 2),
        ("what's the weather like", 3),

        # --- App management ---
        ("open Spotify", 5),
        ("play a good song in Spotify", 8),
        ("pause the music", 3),
        ("close Spotify", 3),

        # --- Browser commands ---
        ("open Chrome", 5),
        ("search for relax music on YouTube", 8),

        # --- File creation (LLM content generation) ---
        ("create a beautiful and functioning calculator using html css and javascript", 15),

        # --- Window management ---
        ("open Chrome and Firefox side by side", 8),
        ("minimize all apps opened right now", 3),

        # --- System commands ---
        ("turn off bluetooth", 5),

        # --- Language / personality ---
        ("introduce yourself to my friends in nepali language", 8),

        # --- System info ---
        ("how much RAM is being used right now", 3),
        ("what are the top processes using CPU", 3),
    ]

    print(f"\nLoading Brain...")
    brain = load_brain()
    print(f"Brain loaded: provider={brain.provider_name}, model={brain.ollama_model}")
    print(f"\nRunning {len(tests)} tests...\n")

    results = []
    for i, (cmd, wait) in enumerate(tests, 1):
        result = run_test(brain, cmd, i, len(tests))
        results.append(result)
        if wait and i < len(tests):
            print(f"  (waiting {wait}s for action to complete...)")
            time.sleep(wait)

    # Summary
    print(f"\n\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")

    ok = sum(1 for r in results if r["status"] == "OK")
    errors = sum(1 for r in results if r["status"] == "ERROR")
    fallbacks = sum(1 for r in results if r["status"] == "FALLBACK")
    exceptions = sum(1 for r in results if r["status"] == "EXCEPTION")

    print(f"  Total: {len(results)}")
    print(f"  OK:         {ok}")
    print(f"  ERROR:      {errors}")
    print(f"  FALLBACK:   {fallbacks}")
    print(f"  EXCEPTION:  {exceptions}")
    print()

    for r in results:
        icon = {"OK": "+", "ERROR": "!", "FALLBACK": "~", "EXCEPTION": "X"}[r["status"]]
        print(f"  [{icon}] {r['command'][:45]:<45} {r['status']:<10} {r['elapsed']:.1f}s  tools={r['tools']}")

    # Save results
    report_path = os.path.join(_root, "test_results.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {report_path}")

    return results


if __name__ == "__main__":
    main()
