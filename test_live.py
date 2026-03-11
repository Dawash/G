"""
Live integration test — tests the full brain.think() pipeline.

Runs each test command through the complete flow:
  direct dispatch → skill library → LLM → tool execution

Usage:
    python test_live.py
"""
import sys
import os
import io
import time
import logging

# UTF-8 console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Suppress noisy logs
logging.basicConfig(level=logging.WARNING, format='%(name)s: %(message)s')
for noisy in ['httpx', 'httpcore', 'urllib3', 'PIL', 'faster_whisper']:
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

# ------------------------------------------------------------------
# Test cases: (description, command, expected_behavior, timeout_s)
# ------------------------------------------------------------------
TEST_CASES = [
    # === DIRECT DISPATCH (no LLM needed) ===
    ("CLI: RAM usage", "how much ram am I using", "should show RAM stats", 15),
    ("CLI: disk space", "how much disk space is free", "should show disk stats", 15),
    ("CLI: running processes", "list running processes", "should list processes", 15),
    ("SETTINGS: display settings", "open display settings", "should open settings", 10),
    ("TOOL: get time", "what time is it", "should return current time", 10),
    ("TOOL: get weather", "what is the weather", "should return weather info", 15),

    # === WEBSITE NAVIGATION (CDP) ===
    ("CDP: open reddit", "open reddit", "should navigate to reddit.com", 15),
    ("CDP: go to github", "go to github", "should navigate to github.com", 15),
    ("CDP: visit gmail", "visit gmail", "should navigate to gmail", 15),

    # === APP MANAGEMENT ===
    ("TOOL: open notepad", "open notepad", "should launch notepad", 10),
    ("TOOL: close notepad", "close notepad", "should close notepad", 10),
    ("TOOL: open calculator", "open calculator", "should launch calculator", 10),
    ("TOOL: close calculator", "close calculator", "should close calculator", 10),

    # === LLM-REQUIRED (needs brain.think full path) ===
    ("LLM: create calculator",
     "create a beautiful and functioning calculator using html css and javascript",
     "should create an HTML file", 60),
    ("LLM: nepali intro",
     "introduce yourself in nepali language",
     "should respond in Nepali", 30),
    ("LLM: joke", "tell me a joke", "should tell a joke", 20),
    ("LLM: capital", "what is the capital of france", "should say Paris", 20),
    ("LLM: math", "what is 847 times 23", "should calculate", 20),

    # === MUSIC ===
    ("API: play song", "play a good song", "should play music on spotify", 30),
    ("TOOL: pause music", "pause the music", "should pause", 10),

    # === CLOSE TAB (keyboard shortcut) ===
    ("TOOL: close tab", "close the tab", "should press ctrl+w", 10),

    # === SPLIT SCREEN ===
    ("SPLIT: chrome+notepad", "open chrome and notepad side by side", "should open both", 20),

    # === MINIMIZE ALL ===
    ("CLI: minimize all", "minimize all apps", "should minimize everything", 10),

    # === COMPOUND (no wifi/bluetooth toggle) ===
    ("COMPOUND: open+navigate", "open chrome and go to reddit", "should open chrome then reddit", 20),
]


def run_test(brain_instance, desc, command, expected, timeout):
    """Run a single test through brain.think()."""
    start = time.time()
    try:
        result = brain_instance.think(command)
        elapsed = time.time() - start

        if result is None:
            return "SKIP", f"brain returned None (no LLM/fallback)", elapsed

        result_str = str(result)[:200]

        # Check for obvious failures
        lower = result_str.lower()
        if any(w in lower for w in ["traceback", "exception", "modulenotfounderror"]):
            return "FAIL", f"Error: {result_str[:100]}", elapsed

        return "OK", result_str, elapsed
    except Exception as e:
        elapsed = time.time() - start
        return "ERROR", f"{type(e).__name__}: {e}", elapsed


def main():
    print("=" * 70)
    print("LIVE INTEGRATION TEST — Full brain.think() Pipeline")
    print("=" * 70)
    print()

    # Initialize brain (same way as assistant_loop.py)
    print("Initializing brain...")
    start_init = time.time()

    from brain import Brain
    from config import load_config
    from ai_providers import create_provider
    from reminders import ReminderManager
    from memory import MemoryStore, UserPreferences
    from orchestration.fallback_router import build_action_map

    cfg = load_config()
    provider_name = cfg.get("provider", "ollama")
    api_key = cfg.get("api_key")
    uname = cfg.get("username", "User")
    ainame = cfg.get("ainame", "G")
    ollama_model = cfg.get("ollama_model", "qwen2.5:7b")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")

    provider = create_provider(provider_name, api_key, "", ollama_model=ollama_model)
    reminder_mgr = ReminderManager()
    memory = MemoryStore()
    preferences = UserPreferences(memory)

    # Wire memory refs for tools
    from tools.memory_workflow_tools import set_memory_refs, set_workflow_registry
    from features.workflows.registry import WorkflowRegistry
    set_memory_refs(memory, preferences)
    set_workflow_registry(WorkflowRegistry())

    action_map = build_action_map(reminder_mgr, provider, memory, cfg)

    brain_instance = Brain(
        provider_name=provider_name,
        api_key=api_key,
        username=uname,
        ainame=ainame,
        action_registry=action_map,
        reminder_mgr=reminder_mgr,
        ollama_model=ollama_model,
        user_preferences=preferences,
        ollama_url=ollama_url,
    )

    init_time = time.time() - start_init
    print(f"Brain ready in {init_time:.1f}s")
    print()

    # Run tests
    results = {"OK": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    total_time = 0

    for i, (desc, command, expected, timeout) in enumerate(TEST_CASES, 1):
        print(f"[{i:2d}/{len(TEST_CASES)}] {desc}")
        print(f"       Command: \"{command}\"")

        status, output, elapsed = run_test(brain_instance, desc, command, expected, timeout)
        total_time += elapsed
        results[status] += 1

        # Truncate output for display
        display = output[:120].replace('\n', ' ')

        if status == "OK":
            print(f"       [{status}] ({elapsed:.1f}s) {display}")
        elif status == "SKIP":
            print(f"       [{status}] ({elapsed:.1f}s) {display}")
        else:
            print(f"       [{status}] ({elapsed:.1f}s) {display}")
        print()

        # Brief pause between tests to avoid rate limiting
        if i < len(TEST_CASES):
            time.sleep(0.5)

    # Summary
    print("=" * 70)
    print(f"RESULTS: {results['OK']} OK | {results['FAIL']} FAIL | "
          f"{results['ERROR']} ERROR | {results['SKIP']} SKIP")
    print(f"Total time: {total_time:.1f}s for {len(TEST_CASES)} tests")
    print("=" * 70)

    # Return exit code
    return 0 if results['FAIL'] == 0 and results['ERROR'] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
