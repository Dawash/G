"""
Self-test diagnostics for G.

Tests core modules, APIs, and subsystems. Triggered by the run_self_test brain tool.
"""

import importlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def _test_import(module_name):
    try:
        importlib.import_module(module_name)
        return True, "OK"
    except Exception as e:
        return False, str(e)


def _test_config():
    try:
        from config import load_config
        cfg = load_config()
        if cfg.get("username") and cfg.get("ai_name"):
            return True, f"user={cfg['username']}, ai={cfg['ai_name']}"
        return True, "Config loadable"
    except Exception as e:
        return False, str(e)


def _test_tts():
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.say("")
        return True, "TTS engine initialized"
    except Exception as e:
        return False, str(e)


def _test_intent():
    try:
        from intent import detect_intent, INTENT_OPEN_APP
        result = detect_intent("open Chrome", use_ai=False)
        if result and result[0][0] == INTENT_OPEN_APP:
            return True, "Intent detection working"
        return False, f"Unexpected: {result}"
    except Exception as e:
        return False, str(e)


def _get_ollama_url():
    """Get the Ollama URL from config, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_URL
        cfg = load_config()
        return cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    except Exception:
        return "http://localhost:11434"


def _test_ollama():
    try:
        import requests
        ollama_url = _get_ollama_url()
        resp = requests.get(ollama_url, timeout=3)
        if resp.status_code == 200:
            tags = requests.get(f"{ollama_url}/api/tags", timeout=5)
            models = [m.get("name", "") for m in tags.json().get("models", [])]
            return True, f"Models: {models}"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def _test_app_finder():
    try:
        from app_finder import get_app_index
        index = get_app_index()
        return (True, f"Found {len(index)} apps") if index else (False, "No apps")
    except Exception as e:
        return False, str(e)


def _test_memory():
    try:
        from memory import MemoryStore
        m = MemoryStore()
        m.log_event("test", "self_test", {"status": "ok"})
        m.close()
        return True, "Read/write OK"
    except Exception as e:
        return False, str(e)


def _test_weather():
    try:
        from weather import get_current_weather
        result = get_current_weather()
        return (True, result[:60]) if result and len(result) > 10 else (False, "Empty")
    except Exception as e:
        return False, str(e)


def _test_reminders():
    try:
        from reminders import ReminderManager
        rm = ReminderManager()
        result = rm.list_active()
        return True, result[:60] if result else "No active reminders"
    except Exception as e:
        return False, str(e)


def _test_brain():
    try:
        from brain import Brain, build_tool_definitions
        from config import load_config, DEFAULT_OLLAMA_MODEL
        cfg = load_config()
        model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL) if cfg else DEFAULT_OLLAMA_MODEL
        tools = build_tool_definitions()
        Brain("ollama", "ollama", "test", "G", {}, ollama_model=model)
        return True, f"{len(tools)} tools"
    except Exception as e:
        return False, str(e)


def _test_whisper():
    try:
        from faster_whisper import WhisperModel
        return True, "faster-whisper available"
    except ImportError:
        return False, "Not installed"
    except Exception as e:
        return False, str(e)


def _test_gtts():
    try:
        from gtts import gTTS
        try:
            import pygame
            return True, "gTTS + pygame"
        except ImportError:
            from speech import _play_mp3_fallback
            return True, "gTTS + PowerShell fallback"
    except ImportError as e:
        return False, str(e)


def _test_cognitive():
    try:
        from cognitive import CognitiveEngine
        cog = CognitiveEngine()
        # Test Phase 1: Learning
        cog.learner.log_outcome("open chrome", "open_app", {"name": "Chrome"}, True, "opened")
        # Test Phase 2: Comprehension
        resolved = cog.resolve_input("open it")
        # Test Phase 3: Decomposition check
        needs = cog.needs_decomposition("open Chrome")
        # Test Phase 4: Confidence
        conf = cog.get_confidence("open Chrome", "open_app")
        cog.close()
        return True, f"All 4 phases OK (conf={conf:.2f})"
    except Exception as e:
        return False, str(e)


def _test_mode_classification():
    """Test that the mode classifier correctly routes quick/agent/research."""
    try:
        from llm.mode_classifier import classify_mode
        cases = [
            ("what time is it", "quick"),
            ("open notepad", "quick"),
            ("play lofi music on youtube", "agent"),
            ("order pizza from dominos", "agent"),
        ]
        failures = []
        for text, expected in cases:
            result = classify_mode(text)
            if result.mode != expected:
                failures.append(f"'{text}' -> {result.mode} (expected {expected})")
        if failures:
            return False, "; ".join(failures)
        return True, f"All {len(cases)} cases routed correctly"
    except Exception as e:
        return False, str(e)


def _test_swarm_init():
    """Test SwarmOrchestrator instantiation with a mock brain."""
    try:
        from agents.orchestrator import SwarmOrchestrator

        class MockBrain:
            action_registry = {}
            def quick_chat(self, prompt):
                return "mock response"

        orch = SwarmOrchestrator(MockBrain())
        agents_ok = []
        for attr, name in [
            ("_planner", "planner"),
            ("_critic", "critic"),
            ("_executor", "executor"),
            ("_researcher", "researcher"),
            ("_memory", "memory"),
        ]:
            if hasattr(orch, attr) and getattr(orch, attr) is not None:
                agents_ok.append(name)
            else:
                return False, f"Missing agent: {name}"
        return True, f"All 5 agents initialized: {', '.join(agents_ok)}"
    except Exception as e:
        return False, str(e)


def _test_planner_classification():
    """Test PlannerAgent._classify() complexity routing."""
    try:
        from agents.planner import PlannerAgent
        from agents.blackboard import Blackboard

        planner = PlannerAgent(llm_fn=lambda p: "", blackboard=Blackboard())
        cases = [
            ("open notepad", "simple"),
            ("open chrome and go to youtube", "compound"),
            ("research flights and book cheapest then email itinerary", "complex"),
        ]
        failures = []
        for text, expected in cases:
            result = planner._classify(text)
            if result != expected:
                failures.append(f"'{text}' -> {result} (expected {expected})")
        if failures:
            return False, "; ".join(failures)
        return True, f"All {len(cases)} classifications correct"
    except Exception as e:
        return False, str(e)


def _test_code_interpreter():
    """Test code execution sandbox: success and safety blocking."""
    try:
        from tools.code_interpreter import execute_code

        # Test 1: valid math
        result = execute_code("print(2+2)")
        if not result["success"]:
            return False, f"Math failed: {result['error']}"
        if result["output"].strip() != "4":
            return False, f"Expected '4', got '{result['output']}'"

        # Test 2: safety — os.system should be blocked
        result2 = execute_code('import os; os.system("dir")')
        if result2["success"]:
            return False, "os.system was NOT blocked (should be)"
        if "safety" not in result2["error"].lower() and "block" not in result2["error"].lower():
            return False, f"Unexpected error type: {result2['error']}"

        return True, "Math OK + os.system blocked"
    except Exception as e:
        return False, str(e)


# All tests
ALL_TESTS = [
    ("Imports: config", lambda: _test_import("config")),
    ("Imports: ai_providers", lambda: _test_import("ai_providers")),
    ("Imports: speech", lambda: _test_import("speech")),
    ("Imports: intent", lambda: _test_import("intent")),
    ("Imports: brain", lambda: _test_import("brain")),
    ("Config", _test_config),
    ("TTS Engine", _test_tts),
    ("Intent Detection", _test_intent),
    ("Ollama LLM", _test_ollama),
    ("App Finder", _test_app_finder),
    ("Memory (SQLite)", _test_memory),
    ("Weather API", _test_weather),
    ("Reminders", _test_reminders),
    ("Brain", _test_brain),
    ("Whisper STT", _test_whisper),
    ("gTTS", _test_gtts),
    ("Cognitive Engine", _test_cognitive),
    ("Mode Classification", _test_mode_classification),
    ("Swarm Init", _test_swarm_init),
    ("Planner Classification", _test_planner_classification),
    ("Code Interpreter", _test_code_interpreter),
]

# Tests safe to run in parallel
_PARALLEL = {
    "Imports: config", "Imports: ai_providers", "Imports: speech",
    "Imports: intent", "Imports: brain", "Config", "Whisper STT", "gTTS",
    "Cognitive Engine", "Mode Classification", "Swarm Init",
    "Planner Classification", "Code Interpreter",
}


def run_self_test():
    """Run all self-tests. Returns human-readable report."""
    results = {}
    passed = failed = 0
    t0 = time.perf_counter()

    parallel = [(n, f) for n, f in ALL_TESTS if n in _PARALLEL]
    sequential = [(n, f) for n, f in ALL_TESTS if n not in _PARALLEL]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn): name for name, fn in parallel}
        for future in as_completed(futures):
            name = futures[future]
            try:
                ok, detail = future.result(timeout=15)
            except Exception as e:
                ok, detail = False, str(e)
            if ok:
                passed += 1
                results[name] = f"  [OK] {name}: {detail}"
            else:
                failed += 1
                results[name] = f"  [FAIL] {name}: {detail}"

    for name, fn in sequential:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)
        if ok:
            passed += 1
            results[name] = f"  [OK] {name}: {detail}"
        else:
            failed += 1
            results[name] = f"  [FAIL] {name}: {detail}"

    elapsed = time.perf_counter() - t0
    total = passed + failed
    header = f"Self-test: {passed}/{total} passed in {elapsed:.1f}s"
    header += ". All systems operational!" if failed == 0 else f". {failed} issue(s)."

    ordered = [results[n] for n, _ in ALL_TESTS if n in results]
    report = header + "\n" + "\n".join(ordered)
    logger.info(report)
    return report


def run_quick_check():
    """Quick health check — critical systems only."""
    critical = {"Ollama LLM", "TTS Engine", "Memory (SQLite)", "Brain"}
    issues = []
    for name, fn in ALL_TESTS:
        if name in critical:
            try:
                ok, detail = fn()
                if not ok:
                    issues.append(f"{name}: {detail}")
            except Exception as e:
                issues.append(f"{name}: {e}")
    return "All critical systems operational." if not issues else "Issues: " + "; ".join(issues)
