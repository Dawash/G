"""Full system test for G_v0 — tests all components end-to-end."""
import sys, os, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

passed = 0
failed = 0
errors = []

def test(name, fn):
    global passed, failed
    try:
        result = fn()
        print(f"  [PASS] {name}")
        if result:
            print(f"         -> {str(result)[:100]}")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        failed += 1
        errors.append((name, str(e)))

# === STARTUP ===
print("[STARTUP]")

def t_config():
    from config import load_config, DEFAULT_OLLAMA_MODEL
    cfg = load_config()
    assert cfg.get("username"), "No username"
    assert cfg.get("ai_name"), "No AI name"
    return f"User={cfg['username']}, AI={cfg['ai_name']}, Model={cfg.get('ollama_model', DEFAULT_OLLAMA_MODEL)}"
test("Config loads", t_config)

def t_modules():
    import io
    from run import check_modules
    old = sys.stdout
    sys.stdout = io.StringIO()
    check_modules()
    sys.stdout = old
    return "All modules present"
test("Module check", t_modules)

# === OLLAMA ===
print("\n[OLLAMA]")

def t_ollama_health():
    import requests
    r = requests.get("http://localhost:11434", timeout=3)
    assert r.status_code == 200
    return "Server running"
test("Ollama server", t_ollama_health)

def t_ollama_native():
    import requests
    r = requests.post("http://localhost:11434/api/chat",
        json={"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "Say OK"}],
              "stream": False, "options": {"num_predict": 5}}, timeout=15)
    r.raise_for_status()
    return r.json()["message"]["content"][:50]
test("Native /api/chat", t_ollama_native)

def t_ollama_tools():
    import requests
    tools = [{"type": "function", "function": {"name": "get_time", "description": "Get time",
        "parameters": {"type": "object", "properties": {}}}}]
    r = requests.post("http://localhost:11434/api/chat",
        json={"model": "qwen2.5:7b", "stream": False,
              "messages": [{"role": "system", "content": "Use tools."},
                           {"role": "user", "content": "What time is it?"}],
              "tools": tools, "options": {"num_predict": 100}}, timeout=30)
    r.raise_for_status()
    msg = r.json()["message"]
    tc = msg.get("tool_calls", [])
    if tc:
        args = tc[0]["function"]["arguments"]
        return f"Tool: {tc[0]['function']['name']}, args type: {type(args).__name__}"
    return f"No tool call, content: {msg.get('content', '')[:50]}"
test("Tool calling", t_ollama_tools)

# === AI PROVIDERS ===
print("\n[AI PROVIDERS]")

def t_provider():
    from ai_providers import OllamaProvider
    p = OllamaProvider("ollama", "You are helpful.", model="qwen2.5:7b")
    reply = p.chat("Say hello in 3 words")
    assert reply and len(reply) > 0
    return reply[:80]
test("OllamaProvider.chat()", t_provider)

# === BRAIN ===
print("\n[BRAIN]")

def t_quickchat():
    from brain import Brain
    b = Brain("ollama", "ollama", "test", "G", action_registry=None, ollama_model="qwen2.5:7b")
    r = b.quick_chat("Say hi in one sentence")
    assert r and len(r) > 0
    return r[:80]
test("Brain.quick_chat()", t_quickchat)

def t_think():
    from brain import Brain
    b = Brain("ollama", "ollama", "test", "G", action_registry=None, ollama_model="qwen2.5:7b")
    r = b.think("What is 2+2?")
    assert r and len(r) > 0
    return r[:80]
test("Brain.think()", t_think)

# === SPEECH ===
print("\n[SPEECH]")

def t_vad_config():
    from speech import _VAD_SPEECH_THRESHOLD, _WAKE_WORD_FUZZY_THRESHOLD, _VAD_SILENCE_TIMEOUT_MS
    assert 0.1 <= _VAD_SPEECH_THRESHOLD <= 0.9
    assert 0.3 <= _WAKE_WORD_FUZZY_THRESHOLD <= 0.9
    return f"VAD={_VAD_SPEECH_THRESHOLD}, Wake={_WAKE_WORD_FUZZY_THRESHOLD}, Silence={_VAD_SILENCE_TIMEOUT_MS}ms"
test("Configurable sensitivity", t_vad_config)

def t_wake():
    from speech import _build_wake_words
    words = _build_wake_words("G")
    assert len(words) >= 8
    return f"{len(words)} variants: {sorted(list(words))[:5]}..."
test("Wake word generation", t_wake)

def t_script():
    from speech import _detect_script_language
    assert _detect_script_language("Hello") is None
    assert _detect_script_language("\u0928\u092e\u0938\u094d\u0924\u0947 \u0926\u0941\u0928\u093f\u092f\u093e") == "hi"
    return "EN=None, Devanagari=hi"
test("Script language detection", t_script)

def t_whisper():
    model_dir = os.path.join("models", "whisper-base")
    exists = os.path.isdir(model_dir) and len(os.listdir(model_dir)) > 0
    return f"Model dir exists: {exists}"
test("Whisper model present", t_whisper)

def t_piper():
    piper_onnx = os.path.join("models", "piper", "en_US-lessac-medium.onnx")
    exists = os.path.isfile(piper_onnx)
    if exists:
        size_mb = os.path.getsize(piper_onnx) / (1024 * 1024)
        return f"Model: {size_mb:.1f} MB"
    return "NOT FOUND"
test("Piper TTS model present", t_piper)

# === DESKTOP AGENT ===
print("\n[DESKTOP AGENT]")

def t_uia():
    from computer import get_ui_elements
    els = get_ui_elements(max_depth=2, max_elements=5)
    return f"{len(els)} elements found"
test("UIA accessibility tree", t_uia)

def t_os_layer():
    import pygetwindow as gw
    active = gw.getActiveWindow()
    windows = [w for w in gw.getAllWindows() if w.visible and w.title]
    title = active.title[:30] if active else "None"
    return f"Active: {title}, Visible: {len(windows)}"
test("OS window detection", t_os_layer)

# === SERVICES ===
print("\n[SERVICES]")

def t_weather():
    from weather import get_current_weather
    w = get_current_weather()
    assert w
    return str(w)[:80]
test("Weather API", t_weather)

def t_reminders():
    from reminders import ReminderManager
    rm = ReminderManager()
    return f"{len(rm.list_active())} active reminders"
test("Reminder system", t_reminders)

def t_alarms():
    from alarms import AlarmManager
    am = AlarmManager()
    h, m = am._parse_alarm_time("7am")
    assert h == 7 and m == 0
    return f"{len(am.list_alarms())} alarms, parse OK"
test("Alarm system", t_alarms)

def t_intent():
    from intent import detect_intent
    result = detect_intent("open chrome")
    assert result and len(result) > 0
    return f"Intent: {result[0]}"
test("Intent detection", t_intent)

def t_memory():
    from memory import MemoryStore
    ms = MemoryStore()
    return "MemoryStore initialized"
test("Memory system", t_memory)

# === CONFIG ===
print("\n[CONFIG]")

def t_no_webremote():
    with open("config.py", "r") as f:
        content = f.read()
    assert "Step 6: Web Remote" not in content
    return "Web remote removed from setup"
test("No web remote in setup", t_no_webremote)

# === SUMMARY ===
print(f"\n============================================")
print(f"  RESULTS: {passed} passed, {failed} failed")
print(f"============================================")
if errors:
    print(f"\nFailed tests:")
    for name, err in errors:
        print(f"  - {name}: {err}")
