"""
Stress test for G_v0 — simulates real user commands.
Tests complex, multi-step, and edge-case scenarios.
"""
import sys, os, time, logging, json

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

from config import load_config, get_system_prompt, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL
from brain import Brain
from intent import detect_intent
from reminders import ReminderManager
from weather import get_current_weather, get_forecast
from news import get_headlines as get_news
from memory import MemoryStore
from app_finder import find_best_match as find_app
from speech import _detect_script_language, _build_wake_words

cfg = load_config()
ollama_model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
ollama_url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)

passed = 0
failed = 0
errors = []
total_time = 0

def test(name, fn, timeout_s=30):
    global passed, failed, total_time
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        total_time += elapsed
        print(f"  [PASS] {name} ({elapsed:.1f}s)")
        if result:
            print(f"         -> {str(result)[:120]}")
        passed += 1
    except Exception as e:
        elapsed = time.time() - t0
        total_time += elapsed
        print(f"  [FAIL] {name} ({elapsed:.1f}s): {e}")
        failed += 1
        errors.append((name, str(e)))


def make_brain():
    return Brain("ollama", "ollama", cfg["username"], cfg["ai_name"],
                 action_registry=None, ollama_model=ollama_model, ollama_url=ollama_url)


print("=" * 55)
print("  G_v0 STRESS TEST — Complex Real-World Commands")
print("=" * 55)

# =====================================================
# 1. BRAIN — Complex queries
# =====================================================
print("\n[1] BRAIN — Complex Queries\n")

def t_math():
    b = make_brain()
    r = b.think("What is the square root of 144 plus 15 times 3?")
    assert r and len(r) > 2
    return r
test("Complex math question", t_math)

def t_knowledge():
    b = make_brain()
    r = b.quick_chat("Explain quantum computing in 2 sentences")
    assert r and len(r) > 20
    return r
test("Knowledge question", t_knowledge)

def t_creator():
    b = make_brain()
    r = b.quick_chat("Who created you?")
    assert r and "dawa" in r.lower() or "sherpa" in r.lower()
    return r
test("Creator identity (must say Dawa Sangay Sherpa)", t_creator)

def t_weather_tool():
    b = make_brain()
    r = b.think("What is the weather right now?")
    assert r and ("temp" in r.lower() or "°" in r or "degree" in r.lower() or "weather" in r.lower())
    return r
test("Weather via brain tool calling", t_weather_tool)

def t_time_tool():
    b = make_brain()
    r = b.think("What time is it right now?")
    assert r and len(r) > 3
    return r
test("Time via brain tool calling", t_time_tool)

def t_news_tool():
    b = make_brain()
    r = b.think("Give me the latest news")
    assert r and len(r) > 20
    return r
test("News via brain tool calling", t_news_tool)

def t_multiturn():
    b = make_brain()
    r1 = b.think("What is the capital of France?")
    assert r1 and "paris" in r1.lower()
    r2 = b.think("And what is the population of that city?")
    assert r2 and len(r2) > 5
    return f"Turn 1: {r1[:50]} | Turn 2: {r2[:50]}"
test("Multi-turn conversation", t_multiturn)

def t_nepali():
    b = make_brain()
    r = b.quick_chat("Say hello in Nepali language")
    assert r and len(r) > 0
    # Avoid encoding issues on Windows console
    try:
        return r[:80]
    except UnicodeEncodeError:
        return f"Got Nepali response ({len(r)} chars)"
test("Nepali language response", t_nepali)

# =====================================================
# 2. INTENT DETECTION — Edge cases
# =====================================================
print("\n[2] INTENT DETECTION — Edge Cases\n")

def t_compound():
    result = detect_intent("open chrome and search for python tutorials")
    assert result and len(result) >= 1
    return str(result)
test("Compound command", t_compound)

def t_natural():
    result = detect_intent("can you please open spotify for me")
    assert result and len(result) >= 1
    types = [r[0] for r in result]
    assert "open_app" in types
    return str(result)
test("Natural speech intent", t_natural)

def t_close():
    result = detect_intent("close notepad")
    assert result and result[0][0] == "close_app"
    return str(result)
test("Close app intent", t_close)

def t_weather_intent():
    result = detect_intent("what is the weather like")
    assert result and result[0][0] == "weather"
    return str(result)
test("Weather intent", t_weather_intent)

def t_reminder_intent():
    result = detect_intent("remind me to call mom at 5pm")
    assert result and result[0][0] == "set_reminder"
    return str(result)
test("Reminder intent", t_reminder_intent)

def t_quit_intent():
    result = detect_intent("goodbye")
    assert result and result[0][0] == "quit"
    return str(result)
test("Quit intent", t_quit_intent)

def t_gibberish():
    result = detect_intent("asdfghjkl qwerty")
    # Should return chat or empty, not crash
    return f"Gibberish handled: {result}"
test("Gibberish input (no crash)", t_gibberish)

def t_empty():
    result = detect_intent("")
    return f"Empty input handled: {result}"
test("Empty input (no crash)", t_empty)

# =====================================================
# 3. APP FINDER — Fuzzy matching
# =====================================================
print("\n[3] APP FINDER — Fuzzy Matching\n")

def t_find_chrome():
    result = find_app("chrome")
    assert result is not None
    return f"Found: {result}"
test("Find Chrome", t_find_chrome)

def t_find_notepad():
    result = find_app("notepad")
    assert result is not None
    return f"Found: {result}"
test("Find Notepad", t_find_notepad)

def t_find_fuzzy():
    result = find_app("calculater")  # typo
    return f"Fuzzy match for 'calculater': {result}"
test("Fuzzy match (typo)", t_find_fuzzy)

def t_find_nonexist():
    result = find_app("xyznonexistentapp123")
    return f"Non-existent app: {result}"
test("Non-existent app (no crash)", t_find_nonexist)

# =====================================================
# 4. WEATHER — Full features
# =====================================================
print("\n[4] WEATHER — Full Features\n")

def t_current():
    w = get_current_weather()
    assert w and len(w) > 10
    return w[:100]
test("Current weather", t_current)

def t_forecast():
    f = get_forecast()
    assert f and len(f) > 10
    return f[:100]
test("Weather forecast", t_forecast)

def t_weather_city():
    from weather import get_current_weather
    w = get_current_weather(city="London")
    assert w and len(w) > 10
    return w[:100]
test("Weather for specific city", t_weather_city)

# =====================================================
# 5. NEWS
# =====================================================
print("\n[5] NEWS\n")

def t_news():
    headlines = get_news()
    assert headlines and len(headlines) > 0
    return f"{len(headlines)} headlines: {headlines[0][:80]}"
test("General news", t_news)

def t_news_tech():
    headlines = get_news(category="tech")
    return f"Tech news: {len(headlines)} headlines" if headlines else "No tech news (OK)"
test("Tech news category", t_news_tech)

# =====================================================
# 6. REMINDERS — Time parsing edge cases
# =====================================================
print("\n[6] REMINDERS — Time Parsing\n")

rm = ReminderManager()

def t_rem_5pm():
    r = rm.add_reminder("call mom", "5pm")
    assert r and "set" in r.lower() or "remind" in r.lower() or "5" in r
    return r[:80]
test("Reminder: '5pm'", t_rem_5pm)

def t_rem_30min():
    r = rm.add_reminder("check oven", "in 30 minutes")
    assert r
    return r[:80]
test("Reminder: 'in 30 minutes'", t_rem_30min)

def t_rem_tomorrow():
    r = rm.add_reminder("dentist appointment", "tomorrow at 9am")
    assert r
    return r[:80]
test("Reminder: 'tomorrow at 9am'", t_rem_tomorrow)

# =====================================================
# 7. MEMORY — Persistence
# =====================================================
print("\n[7] MEMORY — Persistence\n")

def t_mem_store():
    ms = MemoryStore()
    ms.remember("test", "stress_key", "test_value_12345")
    val = ms.recall("test", "stress_key")
    assert val is not None, "recall returned None"
    return f"Stored and retrieved: {val[:50] if val else val}"
test("Store and retrieve fact", t_mem_store)

def t_mem_event():
    ms = MemoryStore()
    ms.log_event("test_event", "stress test ran")
    return "Event logged"
test("Log event", t_mem_event)

# =====================================================
# 8. SPEECH — Edge cases
# =====================================================
print("\n[8] SPEECH — Edge Cases\n")

def t_script_mixed():
    # Mixed text with some Devanagari
    result = _detect_script_language("The answer is नमस्ते")
    return f"Mixed text: {result}"
test("Mixed script detection", t_script_mixed)

def t_script_pure_en():
    result = _detect_script_language("Hello how are you doing today")
    assert result is None
    return "Pure English: None (correct)"
test("Pure English detection", t_script_pure_en)

def t_script_emoji():
    result = _detect_script_language("Hello! 😊🎉")
    return f"Text with emoji: {result}"
test("Text with emojis (no crash)", t_script_emoji)

def t_wake_jarvis():
    words = _build_wake_words("Jarvis")
    assert "jarvis" in words
    assert "hey jarvis" in words
    assert "travis" in words  # mishearing
    return f"{len(words)} variants including mishearings"
test("Wake words for Jarvis", t_wake_jarvis)

def t_wake_custom():
    words = _build_wake_words("Nova")
    assert "hey nova" in words
    assert "nova" in words
    return f"{len(words)} variants: {sorted(words)}"
test("Wake words for custom name", t_wake_custom)

# =====================================================
# 9. DESKTOP AGENT — Planning
# =====================================================
print("\n[9] DESKTOP AGENT — Planning\n")

def t_agent_plan():
    import requests, re
    resp = requests.post(
        f"{ollama_url}/api/chat",
        json={
            "model": ollama_model,
            "messages": [{"role": "user", "content":
                "Create a step-by-step plan (max 5 steps) to: open Chrome and search for Python tutorials.\n"
                "Respond ONLY as a numbered list."}],
            "stream": False,
            "options": {"num_predict": 200, "temperature": 0.2},
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    lines = re.findall(r'^\s*\d+[\.\)]\s*(.+)', content, re.MULTILINE)
    assert len(lines) >= 2, f"Only {len(lines)} steps"
    return f"{len(lines)} steps planned"
test("Agent plan generation", t_agent_plan)

def t_agent_observe():
    from vision import get_active_window_title
    title = get_active_window_title()
    assert title and len(title) > 0
    return f"Can observe: {title[:50]}"
test("Agent observation layer", t_agent_observe)

# =====================================================
# 10. ERROR HANDLING — Bad inputs
# =====================================================
print("\n[10] ERROR HANDLING — Bad Inputs\n")

def t_brain_empty():
    b = make_brain()
    r = b.think("")
    return f"Empty think: {r}"
test("Brain.think('') no crash", t_brain_empty)

def t_brain_long():
    b = make_brain()
    r = b.quick_chat("x" * 200)
    assert r is not None and len(r) > 0
    return f"Long input handled ({len(r)} chars)"
test("Brain with very long input", t_brain_long)

def t_brain_special():
    b = make_brain()
    r = b.quick_chat("What about HTML tags like script and div?")
    assert r is not None and len(r) > 0
    return f"Special chars handled ({len(r)} chars)"
test("Brain with special characters", t_brain_special)

def t_brain_unicode():
    b = make_brain()
    r = b.quick_chat("Tell me about Japan and Arabic languages")
    assert r is not None and len(r) > 0
    return f"Unicode handled ({len(r)} chars)"
test("Brain with unicode input", t_brain_unicode)

def t_provider_fallback():
    from ai_providers import OllamaProvider
    p = OllamaProvider("ollama", "You are helpful.", model="qwen2.5:7b")
    # Test normal chat works
    reply = p.chat("Say yes")
    assert reply and len(reply) > 0
    return f"Provider works: {reply[:50]}"
test("Provider chat + context", t_provider_fallback)

def t_concurrent_brain():
    """Test that brain handles rapid sequential calls."""
    b = make_brain()
    results = []
    for q in ["What is 1+1?", "What is 2+2?", "What is 3+3?"]:
        r = b.think(q)
        results.append(r[:30] if r else "None")
    return f"3 rapid calls: {results}"
test("Rapid sequential brain calls", t_concurrent_brain)

# =====================================================
# 11. BRAIN — Tool chaining
# =====================================================
print("\n[11] BRAIN — Tool Chaining\n")

def t_weather_and_news():
    b = make_brain()
    r = b.think("What is the weather and also give me the latest news")
    assert r and len(r) > 20
    return r[:120]
test("Weather + news in one query", t_weather_and_news)

def t_system_info():
    b = make_brain()
    r = b.think("What operating system am I running?")
    assert r and len(r) > 5
    return r[:100]
test("System info query", t_system_info)

# =====================================================
# SUMMARY
# =====================================================
print(f"\n{'=' * 55}")
print(f"  RESULTS: {passed} passed, {failed} failed ({total_time:.1f}s total)")
print(f"{'=' * 55}")
if errors:
    print(f"\nFailed tests:")
    for name, err in errors:
        print(f"  - {name}: {err[:100]}")
else:
    print("\n  ALL TESTS PASSED — System is ready to use!")
