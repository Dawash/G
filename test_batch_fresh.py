"""Fresh test batch — validates all recent fixes."""
import sys, os, time
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Test commands covering all fix areas
COMMANDS = [
    # === Knowledge (quick_chat bypass) ===
    "what is RAM",
    "explain what an API is",
    # === Time/Date routing fix ===
    "what time is it",
    "what is the date today",
    # === System queries (direct dispatch) ===
    "how much disk space do I have",
    "what is my ip address",
    "check my cpu",
    "how much RAM do I have",
    # === App operations (name normalization) ===
    "open notepad",
    "close notepad",
    "open calculator",
    "close calculator",
    # === Weather/News ===
    "what is the weather",
    "get me the news",
    # === File operations ===
    "list files on my desktop",
    # === Agent mode (timeout fix + caching + strategy history) ===
    "search for Python tutorials on YouTube",
    # === Settings fast-path ===
    "open wifi settings",
    # === Reminders ===
    "set a reminder for 11pm to sleep",
    "list my reminders",
    # === Create file ===
    "create a simple hello world html page",
    # === Mixed ===
    "open chrome",
    "close chrome",
    "what is the capital of France",
    "tell me a joke",
    "who invented the telephone",
]

def main():
    from brain import Brain
    from config import load_config, get_system_prompt, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL

    cfg = load_config()
    model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)
    provider = cfg.get("provider", "ollama")
    api_key = cfg.get("api_key", "")
    username = cfg.get("username", "User")
    ainame = cfg.get("ai_name", "G")

    # Build a minimal action registry so close_app/open_app actually work
    from actions import open_application, close_window, minimize_window, google_search
    action_registry = {
        "open_app": open_application,
        "close_app": close_window,
        "minimize_app": minimize_window,
        "google_search": google_search,
    }
    brain = Brain(
        provider_name=provider, api_key=api_key,
        username=username, ainame=ainame,
        action_registry=action_registry, reminder_mgr=None,
        ollama_model=model, ollama_url=url,
    )
    print(f"Brain ready (model={model})")
    print(f"Running {len(COMMANDS)} commands...\n")

    results = {"ok": 0, "fail": 0, "errors": []}
    total_start = time.time()

    for i, cmd in enumerate(COMMANDS, 1):
        t0 = time.perf_counter()
        try:
            resp = brain.think(cmd)
            elapsed = time.perf_counter() - t0
            resp_str = str(resp or "")
            preview = resp_str.replace('\n', ' ')[:100]

            # Check for known failure patterns
            failed = False
            if cmd == "what time is it" and "don't have" in resp_str.lower():
                failed = True
                reason = "routed to quick_chat instead of get_time"
            elif cmd == "what is the date today" and "don't have" in resp_str.lower():
                failed = True
                reason = "routed to quick_chat instead of get_time"
            elif "error" in resp_str.lower()[:50] and "timed out" in resp_str.lower():
                failed = True
                reason = "timeout"
            elif not resp_str.strip():
                failed = True
                reason = "empty response"
            else:
                reason = ""

            if failed:
                results["fail"] += 1
                results["errors"].append((cmd, reason))
                print(f"[{i}/{len(COMMANDS)}] FAIL ({elapsed:.1f}s) \"{cmd}\" -> {reason}")
            else:
                results["ok"] += 1
                print(f"[{i}/{len(COMMANDS)}] OK ({elapsed:.1f}s) \"{cmd}\" -> {preview}")

        except Exception as e:
            elapsed = time.perf_counter() - t0
            results["fail"] += 1
            results["errors"].append((cmd, str(e)[:80]))
            print(f"[{i}/{len(COMMANDS)}] ERROR ({elapsed:.1f}s) \"{cmd}\" -> {e}")

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"RESULTS: {results['ok']}/{len(COMMANDS)} OK, {results['fail']} FAIL")
    print(f"Total time: {total_elapsed:.0f}s ({total_elapsed/len(COMMANDS):.1f}s avg)")
    if results["errors"]:
        print(f"\nFailed commands:")
        for cmd, reason in results["errors"]:
            print(f"  - \"{cmd}\": {reason}")

if __name__ == "__main__":
    main()
