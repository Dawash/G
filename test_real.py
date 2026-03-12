"""Real-world brain test — feeds commands through Brain.think() and reports results."""
import sys, os, time, logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace") if sys.platform == "win32" else None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from config import load_config, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL
from brain import Brain

cfg = load_config()
ollama_model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
ollama_url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)

def p(msg):
    print(msg, flush=True)

p(f"Model: {ollama_model} | URL: {ollama_url}")
p("=" * 60)

b = Brain("ollama", "ollama", cfg["username"], cfg["ai_name"],
          action_registry=None, ollama_model=ollama_model, ollama_url=ollama_url)

commands = [
    # --- Info tools ---
    "What time is it?",
    "What is the weather right now?",
    "Give me the latest news",
    "What is 15 times 23?",
    "Who created you?",
    # --- System queries (direct dispatch) ---
    "Check how much RAM my computer has",
    "What is my IP address?",
    "How much disk space do I have?",
    # --- Knowledge (quick_chat) ---
    "What is the capital of France?",
    "Explain quantum computing in one sentence",
    # --- Reminders ---
    "Set a reminder for 5pm to drink water",
    "List my reminders",
    # --- Edge cases ---
    "",
    "asdfghjkl",
    "x" * 100,
    # --- Multi-turn ---
    "What is the population of Tokyo?",
    # --- More complex ---
    "Tell me a joke",
    "What is the weather forecast for tomorrow?",
    "Set a reminder to call mom in 30 minutes",
    # --- Complex agent-worthy ---
    "Search Google for Python tutorials",
    "What apps are using the most RAM?",
    # --- Interactive choice scenarios (just test LLM tool selection) ---
    "I have 3 Gmail accounts: john@gmail.com, work@gmail.com, personal@gmail.com. Which should I use?",
    # --- Conversational ---
    "How are you today?",
    "Thank you for helping me",
    "What can you do?",
]

passed = 0
failed = 0
errors = []

for i, cmd in enumerate(commands, 1):
    t0 = time.time()
    display = cmd[:45] if cmd else "(empty)"
    try:
        r = b.think(cmd) if cmd.strip() else b.think(cmd)
        elapsed = time.time() - t0

        if r is None and cmd.strip():
            r = b.quick_chat(cmd)
            elapsed = time.time() - t0

        if r and len(str(r)) > 2:
            p(f"  [{i:2d}] PASS ({elapsed:.1f}s) \"{display}\"")
            p(f"        -> {str(r)[:100]}")
            passed += 1
        elif not cmd.strip():
            p(f"  [{i:2d}] PASS ({elapsed:.1f}s) \"{display}\" -> None (expected)")
            passed += 1
        else:
            p(f"  [{i:2d}] WARN ({elapsed:.1f}s) \"{display}\" -> {repr(r)[:60]}")
            passed += 1
    except Exception as e:
        elapsed = time.time() - t0
        p(f"  [{i:2d}] FAIL ({elapsed:.1f}s) \"{display}\" -> {str(e)[:80]}")
        failed += 1
        errors.append((display, str(e)[:100]))

p(f"\n{'=' * 60}")
p(f"Results: {passed} passed, {failed} failed out of {len(commands)}")
if errors:
    p("Errors:")
    for cmd, err in errors:
        p(f"  - \"{cmd}\": {err}")
else:
    p("ALL COMMANDS PASSED!")
p(f"{'=' * 60}")
