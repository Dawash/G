#!/usr/bin/env python3
"""
G — Personal AI Operating System
=================================

Single-file launcher. Run this to start everything:

    python run.py

What it does:

  1. Checks Python version
  2. Auto-installs missing dependencies
  3. Sets up Ollama (local LLM brain) if not installed
  4. Validates all modules can import
  5. Launches the assistant

First run will prompt for:
  - Your name
  - AI assistant name
  - AI provider (Ollama recommended — free, local, no limits)
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

# Fix Windows console encoding for multilingual output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ===================================================================
# Configuration
# ===================================================================

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MIN_PYTHON = (3, 10)
PREFERRED_PYTHON = (3, 12)

REQUIRED_PACKAGES = {
    "pyttsx3": "pyttsx3",
    "speech_recognition": "SpeechRecognition",
    "requests": "requests",
    "pygetwindow": "pygetwindow",
    "pyautogui": "pyautogui",         # Desktop automation (keyboard/mouse)
    "PIL": "Pillow",                  # Screenshot processing for vision
    "numpy": "numpy",                 # Audio/numeric processing
    "rapidfuzz": "rapidfuzz",         # Fuzzy app matching
    "cryptography": "cryptography>=41.0.0",  # Credential encryption
    "psutil": "psutil>=5.9.0",        # System monitoring
    "pyperclip": "pyperclip",         # Clipboard access
}

OPTIONAL_PACKAGES = {
    "pyaudio": "PyAudio",             # Microphone input (can fail on some systems)
    "win32com.client": "pywin32",     # Start Menu shortcut resolution
    "comtypes": "comtypes",           # UIA accessibility tree (pywinauto dep)
    "pywinauto": "pywinauto",         # Windows UI Automation
    "gtts": "gTTS",                   # Google TTS (online, multilingual)
    "pygame": "pygame",               # Audio playback for TTS
    "websockets": "websockets>=12.0", # WebSocket gateway
}

CORE_MODULES = [
    "config",
    "ai_providers",
    "speech",
    "intent",
    "actions",
    "app_finder",
    "memory",
    "weather",
    "reminders",
    "news",
    "brain",
    "computer",
    "vision",
    "desktop_agent",
    "assistant",
]

# Ollama settings
OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_DEFAULT_MODEL = "qwen2.5:7b"
_DEFAULT_OLLAMA_API = "http://localhost:11434"


def _get_ollama_api():
    """Get Ollama API URL from config.json if available, else use default."""
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("ollama_url", _DEFAULT_OLLAMA_API).rstrip("/")
    except Exception:
        pass
    return _DEFAULT_OLLAMA_API


OLLAMA_API = _get_ollama_api()


# ===================================================================
# Helpers
# ===================================================================

def print_banner():
    try:
        print("""
  ╔══════════════════════════════════════════╗
  ║     G — Personal AI Operating System     ║
  ║   Voice-first · Smart · Self-improving   ║
  ╚══════════════════════════════════════════╝
        """)
    except UnicodeEncodeError:
        print("\n  G -- Personal AI Operating System")
        print("  Voice-first | Smart | Self-improving\n")


def check_python():
    """Verify Python version."""
    ver = sys.version_info[:2]
    if ver < MIN_PYTHON:
        print(f"[ERROR] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required. You have {ver[0]}.{ver[1]}.")
        sys.exit(1)
    print(f"[OK] Python {ver[0]}.{ver[1]}")


def install_package(pip_name):
    """Install a package via pip."""
    print(f"  Installing {pip_name}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", pip_name, "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8", errors="replace",
    )
    return result.returncode == 0


def _deps_cache_valid():
    """Check if dependency cache is still valid (requirements.txt unchanged)."""
    cache_file = os.path.join(PROJECT_DIR, ".deps_ok")
    req_file = os.path.join(PROJECT_DIR, "requirements.txt")
    if not os.path.isfile(cache_file) or not os.path.isfile(req_file):
        return False
    try:
        import hashlib
        with open(req_file, "rb") as f:
            req_hash = hashlib.md5(f.read()).hexdigest()
        with open(cache_file, "r") as f:
            cached = f.read().strip()
        return cached == f"{sys.executable}:{req_hash}"
    except Exception:
        return False


def _write_deps_cache():
    """Write dependency cache marker."""
    try:
        import hashlib
        req_file = os.path.join(PROJECT_DIR, "requirements.txt")
        with open(req_file, "rb") as f:
            req_hash = hashlib.md5(f.read()).hexdigest()
        with open(os.path.join(PROJECT_DIR, ".deps_ok"), "w") as f:
            f.write(f"{sys.executable}:{req_hash}")
    except Exception:
        pass


def check_dependencies():
    """Check and install missing dependencies.

    Strategy: skip pip if cache says deps are up to date,
    otherwise try full requirements.txt then verify individually.
    """
    print("\n[DEPS] Checking dependencies...")

    # --- Fast path: if requirements.txt hasn't changed, skip pip entirely ---
    if _deps_cache_valid():
        # Quick verify: spot-check a few key imports
        try:
            import requests, numpy, PIL, rapidfuzz  # noqa: F401
            print("  [OK] All packages verified (cached)")
            print("[OK] Dependencies ready\n")
            return
        except ImportError:
            pass  # Cache stale, fall through

    # --- Step 1: Full requirements.txt install (catches everything) ---
    req_file = os.path.join(PROJECT_DIR, "requirements.txt")
    if os.path.isfile(req_file):
        print("  Installing from requirements.txt...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            print("  [OK] All packages from requirements.txt installed")
        else:
            print("  [WARN] Some packages from requirements.txt failed — checking individually...")

    # --- Step 2: Verify required packages individually (safety net) ---
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append((import_name, pip_name))

    if missing:
        print(f"\n  {len(missing)} required package(s) still missing. Installing...")
        for import_name, pip_name in missing:
            if install_package(pip_name):
                print(f"  [OK] {pip_name} installed")
            else:
                print(f"  [FAIL] Could not install {pip_name}")
                print(f"         Try: pip install {pip_name}")
    else:
        print("  [OK] All required packages verified")

    # --- Step 3: Optional packages — install silently, don't block ---
    for import_name, pip_name in OPTIONAL_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"  [OPTIONAL] Installing {pip_name}...")
            if not install_package(pip_name):
                if pip_name == "PyAudio":
                    print(f"  [NOTE] PyAudio failed to install. Microphone may not work.")
                    print(f"         On Windows, try: pip install pyaudio --only-binary=:all:")

    _write_deps_cache()
    print("[OK] Dependencies ready\n")


# ===================================================================
# Ollama — built-in local LLM brain
# ===================================================================

def _ollama_is_installed():
    """Check if Ollama is installed."""
    if shutil.which("ollama"):
        return True
    # Check common install paths on Windows
    for path in [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
    ]:
        if os.path.isfile(path):
            return True
    return False


def _ollama_is_running():
    """Check if Ollama server is running."""
    try:
        req = urllib.request.Request(OLLAMA_API, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_has_model(model_name):
    """Check if a model is already pulled."""
    try:
        req = urllib.request.Request(f"{OLLAMA_API}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            # Match with or without tag: "qwen2.5:7b" matches "qwen2.5:7b" or "qwen2.5"
            model_base = model_name.split(":")[0]
            return model_name in models or any(m.split(":")[0] == model_base for m in models)
    except Exception:
        return False


def _start_ollama():
    """Start the Ollama server."""
    print("  Starting Ollama server...")
    try:
        # Try to start ollama serve in background
        if shutil.which("ollama"):
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
        else:
            # Try the full path
            for path in [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
            ]:
                if os.path.isfile(path):
                    subprocess.Popen(
                        [path, "serve"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x00000008,
                    )
                    break

        # Wait for server to start
        for _ in range(15):
            time.sleep(1)
            if _ollama_is_running():
                print("  [OK] Ollama server started")
                return True
        print("  [WARN] Ollama server didn't start in time")
        return False
    except Exception as e:
        print(f"  [WARN] Could not start Ollama: {e}")
        return False


def _pull_model(model_name):
    """Pull a model using Ollama CLI."""
    print(f"  Pulling model '{model_name}'... (this may take a few minutes on first run)")
    try:
        result = subprocess.run(
            ["ollama", "pull", model_name],
            timeout=600,  # 10 min max
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  [WARN] Could not pull model: {e}")
        return False


def setup_ollama():
    """
    Ensure Ollama is installed, running, and has a model.
    This is the built-in local brain for G.
    """
    print("[BRAIN] Setting up local AI brain (Ollama)...")

    # Check config — maybe user chose a different provider
    config_file = os.path.join(PROJECT_DIR, "config.json")
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            config = json.load(f)
        if config.get("provider") not in ("ollama", None):
            # User chose a cloud provider — still check Ollama as fallback
            if _ollama_is_installed():
                print(f"  [OK] Ollama installed (available as fallback)")
                if not _ollama_is_running():
                    _start_ollama()
            else:
                print(f"  [INFO] Using {config.get('provider')} — Ollama not needed")
            return

    # Step 1: Check if Ollama is installed
    if not _ollama_is_installed():
        print("\n  Ollama is not installed. It's the free local AI brain for G.")
        print("  Without it, G needs a cloud API key (OpenAI/Anthropic).")
        print()
        print("  To install Ollama:")
        print("    1. Download from https://ollama.com/download")
        print("    2. Run the installer")
        print("    3. Run this script again")
        print()

        choice = input("  Do you want me to download and install Ollama now? (y/n): ").strip().lower()
        if choice in ("y", "yes"):
            print("  Downloading Ollama installer...")
            installer_path = os.path.join(
                os.environ.get("USERPROFILE", ""), "Downloads", "OllamaSetup.exe"
            )
            try:
                urllib.request.urlretrieve(OLLAMA_URL, installer_path)
                print(f"  Downloaded to: {installer_path}")
                print("  Launching installer...")
                subprocess.Popen([installer_path], shell=True)
                print("  Installer launched — complete it, then re-run: python run.py")
                sys.exit(0)
            except Exception as e:
                print(f"  Download failed: {e}")
                print("  Download manually from https://ollama.com/download")
        else:
            print("  Continuing without Ollama. You'll need a cloud API key.")
        return

    print("  [OK] Ollama installed")

    # Step 2: Ensure server is running
    if not _ollama_is_running():
        _start_ollama()

    if not _ollama_is_running():
        print("  [WARN] Ollama server not running. Start it with: ollama serve")
        return

    print("  [OK] Ollama server running")

    # Step 3: Check for model
    model = OLLAMA_DEFAULT_MODEL
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            config = json.load(f)
        model = config.get("ollama_model", OLLAMA_DEFAULT_MODEL)

    if _ollama_has_model(model):
        print(f"  [OK] Model '{model}' ready")
    else:
        print(f"  Model '{model}' not found locally.")
        _pull_model(model)
        if _ollama_has_model(model):
            print(f"  [OK] Model '{model}' ready")
        else:
            print(f"  [WARN] Model '{model}' not available. Pull it with: ollama pull {model}")

    # After primary model confirmed ready, check multi-tier routing models
    ollama_url = OLLAMA_API
    ensure_essential_models(ollama_url, model)

    print("[OK] Local AI brain ready\n")


_MODELS_CHECK_FILE = os.path.join(PROJECT_DIR, "data", ".models_checked")
_MODELS_CHECK_INTERVAL = 7 * 86400  # 7 days

def _should_check_models():
    """Only check/download models once per week."""
    try:
        if os.path.exists(_MODELS_CHECK_FILE):
            if time.time() - os.path.getmtime(_MODELS_CHECK_FILE) < _MODELS_CHECK_INTERVAL:
                return False
    except Exception:
        pass
    return True

def _mark_models_checked():
    os.makedirs(os.path.join(PROJECT_DIR, "data"), exist_ok=True)
    with open(_MODELS_CHECK_FILE, "w") as f:
        f.write(str(time.time()))

def ensure_essential_models(ollama_url, primary_model):
    """Download essential models for multi-tier routing if missing."""
    if not _should_check_models():
        return

    import requests as _req
    try:
        r = _req.get(f"{ollama_url}/api/tags", timeout=10)
        if r.status_code != 200:
            return
        installed = [m["name"].lower() for m in r.json().get("models", [])]
    except Exception:
        return

    primary_lower = primary_model.lower()

    # Check fast tier: need a small model for classification
    _fast_candidates = ["qwen2.5:7b", "qwen2.5:3b", "gemma3:4b", "phi3:mini", "llama3.2:3b"]
    _fast_keywords = ["qwen2.5:7b", "qwen2.5:3b", "gemma3:4b", "gemma2:2b", "phi3", "llama3.2:3b"]
    has_fast = any(any(k in m for k in _fast_keywords) for m in installed)
    # Primary model might itself be small enough
    if not has_fast and any(s in primary_lower for s in ["3b", "4b", "7b", "8b", "mini"]):
        has_fast = True

    # Check vision tier
    has_vision = any(any(v in m for v in ["llava", "moondream", "bakllava"]) for m in installed)

    to_pull = []
    if not has_fast:
        to_pull.append(("fast", "qwen2.5:7b"))
    if not has_vision:
        to_pull.append(("vision", "llava:7b"))

    if not to_pull:
        _mark_models_checked()
        return

    print("\n  [MODELS] Multi-model routing needs additional models:")
    for tier, model in to_pull:
        print(f"    - {model} ({tier} tier)")

    for tier, model in to_pull:
        print(f"  Pulling {model}...")
        try:
            # Use subprocess to show progress (ollama pull has nice output)
            import subprocess as _sp
            result = _sp.run(
                ["ollama", "pull", model],
                timeout=600,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                print(f"  [OK] {model} ready")
            else:
                print(f"  [WARN] Failed to pull {model} — run manually: ollama pull {model}")
        except _sp.TimeoutExpired:
            print(f"  [WARN] {model} download timed out — run manually: ollama pull {model}")
        except Exception as e:
            print(f"  [WARN] {model} download failed: {e}")

    _mark_models_checked()


def _check_vision_model():
    """Check if the llava vision model is available, offer to auto-pull."""
    if not _ollama_is_running():
        return
    if _ollama_has_model("llava"):
        print("  [OK] Vision model 'llava' available (screen vision enabled)")
    else:
        print("  [INFO] Vision model 'llava' not installed.")
        print("         Screen vision features (take_screenshot, agent_task) require it.")
        try:
            choice = input("  Pull llava now? (~4GB, needed for vision) (y/n): ").strip().lower()
            if choice in ("y", "yes"):
                _pull_model("llava")
                if _ollama_has_model("llava"):
                    print("  [OK] Vision model 'llava' ready")
                else:
                    print("  [WARN] llava pull failed. To retry: ollama pull llava")
            else:
                print("         To install later: ollama pull llava")
        except (EOFError, KeyboardInterrupt):
            print("\n         Skipped. To install later: ollama pull llava")


def _validate_cloud_api_key(provider, api_key, config, config_file):
    """
    Background worker: test a cloud API key with an actual completion.
    Called from a thread so it doesn't block startup.
    Stores result in _provider_validation_result for later check.
    """
    global _provider_validation_result

    test_urls = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        "anthropic": "https://api.anthropic.com/v1/messages",
    }

    url = test_urls.get(provider)
    if not url:
        _provider_validation_result = ("ok", None)
        return

    try:
        if provider == "anthropic":
            body = json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]
            }).encode()
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2024-06-01")
            req.add_header("Content-Type", "application/json")
        else:
            body = json.dumps({
                "model": "gpt-4o-mini" if provider == "openai" else "gpt-3.5-turbo",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]
            }).encode()
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=15) as resp:
            _provider_validation_result = ("ok", None)
            return

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode()
        except Exception:
            pass

        if e.code == 401:
            _provider_validation_result = ("dead", "INVALID (authentication failed)")
        elif e.code == 429 or "insufficient_quota" in error_body:
            _provider_validation_result = ("dead", "NO CREDITS / QUOTA EXHAUSTED")
        else:
            # Might be temporary (5xx, etc.)
            _provider_validation_result = ("warn", f"HTTP {e.code}")
            return

    except urllib.error.URLError:
        _provider_validation_result = ("warn", "Could not reach API. Check your internet.")
        return

    except Exception as e:
        _provider_validation_result = ("warn", f"Validation failed: {e}")
        return


# Global for background validation result: ("ok"|"warn"|"dead", message)
_provider_validation_result = None
_provider_validation_thread = None


def validate_provider():
    """
    Validate the configured AI provider works.
    For Ollama: quick local check (fast).
    For cloud providers: launches background thread to test API key.
    """
    global _provider_validation_thread

    config_file = os.path.join(PROJECT_DIR, "config.json")
    if not os.path.exists(config_file):
        return  # First run — setup will handle it

    with open(config_file, "r") as f:
        config = json.load(f)

    provider = config.get("provider", "ollama")
    api_key = config.get("api_key", "")

    if provider == "ollama":
        # Check Ollama is reachable (fast, local)
        if _ollama_is_running():
            print("[OK] Ollama provider ready")
        else:
            print("[WARN] Ollama not running. Trying to start...")
            _start_ollama()
            if not _ollama_is_running():
                print("[WARN] Could not reach Ollama. Start it with: ollama serve")
        return

    if not api_key or api_key == "ollama":
        return

    # Cloud provider: validate API key in background thread
    print(f"[BRAIN] Validating {provider} API key (background)...")
    _provider_validation_thread = threading.Thread(
        target=_validate_cloud_api_key,
        args=(provider, api_key, config, config_file),
        daemon=True,
    )
    _provider_validation_thread.start()


def _finish_provider_validation():
    """
    Wait for background provider validation to complete (if running).
    If the key is dead, offer to switch provider.
    Called just before launch so user interaction is possible.
    """
    global _provider_validation_thread, _provider_validation_result

    if _provider_validation_thread is None:
        return

    # Wait up to 15s for the background check to finish
    _provider_validation_thread.join(timeout=15)

    if _provider_validation_result is None:
        print("  [WARN] Provider validation timed out. Continuing anyway.")
        return

    status, message = _provider_validation_result

    # Read config again for provider name
    config_file = os.path.join(PROJECT_DIR, "config.json")
    with open(config_file, "r") as f:
        config = json.load(f)
    provider = config.get("provider", "unknown")

    if status == "ok":
        print(f"[OK] {provider} API key valid")
        return

    if status == "warn":
        print(f"  [WARN] {provider}: {message}")
        return

    # status == "dead" — key is not working
    print(f"\n  [ERROR] {provider} API key: {message}")
    print(f"         The Brain cannot function without a working API.")

    print(f"\n  Your {provider} API key is not working.")
    print(f"  G needs a working AI brain to understand your requests.\n")
    print(f"  Options:")
    print(f"    1. Switch to Ollama (FREE, local, no limits)")
    print(f"    2. Enter a new {provider} API key")
    print(f"    3. Continue anyway (limited to basic commands)")

    choice = input(f"\n  Pick (1/2/3): ").strip()

    if choice == "1":
        config["provider"] = "ollama"
        config["api_key"] = "ollama"
        if "ollama_model" not in config:
            model = input(f"  Which Ollama model? (default: {OLLAMA_DEFAULT_MODEL}): ").strip()
            config["ollama_model"] = model or OLLAMA_DEFAULT_MODEL
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        print("  [OK] Switched to Ollama!")
        if not _ollama_is_running():
            _start_ollama()

    elif choice == "2":
        new_key = input(f"  Enter your new {provider} API key: ").strip()
        if new_key:
            config["api_key"] = new_key
            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)
            print("  [OK] API key updated!")

    else:
        print("  Continuing with basic commands only (no AI brain).")


def check_modules():
    """
    Verify all project modules exist as files.
    Actual imports happen when the assistant starts — this is a fast
    existence check to catch missing files early without the overhead
    of importing heavy modules (speech, brain, etc.) twice.
    """
    print("[MODULES] Checking project modules...")

    failed = []
    for module in CORE_MODULES:
        # Check for module.py or module/ (package directory)
        py_file = os.path.join(PROJECT_DIR, f"{module}.py")
        pkg_dir = os.path.join(PROJECT_DIR, module, "__init__.py")
        if os.path.isfile(py_file) or os.path.isfile(pkg_dir):
            print(f"  [OK] {module}")
        else:
            print(f"  [FAIL] {module}: file not found ({module}.py)")
            failed.append(module)

    if failed:
        print(f"\n[ERROR] {len(failed)} module(s) missing: {', '.join(failed)}")
        print("Fix the errors above and try again.")
        sys.exit(1)

    # Optional modules (not required, but nice to have)
    for opt_module in ["alarms", "cognitive", "skills", "agent_router"]:
        py_file = os.path.join(PROJECT_DIR, f"{opt_module}.py")
        pkg_dir = os.path.join(PROJECT_DIR, opt_module, "__init__.py")
        if os.path.isfile(py_file) or os.path.isfile(pkg_dir):
            print(f"  [OK] {opt_module} (optional)")
        else:
            print(f"  [INFO] {opt_module} not found (optional, not required)")

    print("[OK] All modules ready\n")


def launch():
    """Launch the assistant (voice-only or GUI dashboard)."""
    gui_mode = "--gui" in sys.argv

    os.chdir(PROJECT_DIR)
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    if gui_mode:
        print("[LAUNCH] Starting Jarvis Dashboard...\n")
        print("=" * 50)
        try:
            from main_gui import main as gui_main
            gui_main()
            return
        except ImportError as e:
            print(f"\n[WARN] GUI mode not available: {e}")
            print("Falling back to terminal mode.\n")
            # Fall through to terminal mode below
        except Exception as e:
            print(f"\n[CRASH] {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # Terminal mode (default, or fallback from failed GUI import)
    print("[LAUNCH] Starting G...\n")
    print("=" * 50)
    from assistant import run
    try:
        run()
    except KeyboardInterrupt:
        print("\n\nShutting down gracefully...")
    except Exception as e:
        print(f"\n[CRASH] {e}")
        print("Check assistant.log for details.")
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ===================================================================
# Main
# ===================================================================

def _relaunch_with_preferred_python():
    """
    If not running on the preferred Python version, try to re-launch
    with 'py -3.12' (Windows py launcher). Returns True if re-launched.
    """
    current = sys.version_info[:2]
    if current == PREFERRED_PYTHON:
        return False  # Already on the right version

    # Try the Windows py launcher
    py_launcher = shutil.which("py")
    if not py_launcher:
        return False

    target = f"-{PREFERRED_PYTHON[0]}.{PREFERRED_PYTHON[1]}"
    # Check if py -3.12 actually exists
    try:
        result = subprocess.run(
            [py_launcher, target, "--version"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            return False
    except Exception:
        return False

    # Re-launch this script with the preferred Python
    print(f"[INFO] Python {current[0]}.{current[1]} detected — "
          f"re-launching with Python {PREFERRED_PYTHON[0]}.{PREFERRED_PYTHON[1]}...\n")
    try:
        ret = subprocess.call([py_launcher, target, os.path.abspath(__file__)] + sys.argv[1:])
        sys.exit(ret)
    except Exception:
        return False  # Fall through to current Python


def _ensure_first_run_setup():
    """
    On first run, trigger interactive config setup BEFORE downloading models.
    This ensures the user's chosen model gets downloaded, not the default.
    Returns True if first-run setup was just completed.
    """
    config_file = os.path.join(PROJECT_DIR, "config.json")
    if os.path.exists(config_file):
        return False  # Already configured

    print("\n  First-time setup detected! Let's configure your assistant.\n")

    # Import config module and trigger interactive setup
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)
    os.chdir(PROJECT_DIR)
    try:
        from config import load_config
        load_config()  # This triggers _setup_new() if no config.json
        return True
    except Exception as e:
        print(f"  [WARN] Setup error: {e}")
        print("  You can re-run setup by deleting config.json and restarting.")
        return False


def _download_speech_models():
    """Pre-download speech models (Whisper STT + Piper TTS) so first use is instant."""
    print("[SPEECH] Downloading speech models...")

    # --- Whisper STT model ---
    whisper_dir = os.path.join(PROJECT_DIR, "models", "whisper-base")
    if os.path.isdir(whisper_dir) and os.listdir(whisper_dir):
        print("  [OK] Whisper STT model ready")
    else:
        print("  Downloading Whisper STT model (first-time, ~150MB)...")
        try:
            from faster_whisper import WhisperModel
            # Download to default cache, speech.py will copy to local dir on first use
            _model = WhisperModel("base", device="cpu", compute_type="int8")
            del _model  # Free memory immediately
            print("  [OK] Whisper STT model downloaded")
        except ImportError:
            print("  [SKIP] faster-whisper not installed — STT will use fallback")
        except Exception as e:
            print(f"  [WARN] Could not pre-download Whisper model: {e}")
            print("         Speech-to-text will download on first use.")

    # --- Piper TTS model ---
    piper_dir = os.path.join(PROJECT_DIR, "models", "piper")
    piper_onnx = os.path.join(piper_dir, "en_US-lessac-medium.onnx")
    if os.path.isfile(piper_onnx):
        print("  [OK] Piper TTS model ready")
    else:
        print("  Downloading Piper TTS voice (~60MB)...")
        try:
            os.makedirs(piper_dir, exist_ok=True)
            base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
            model_name = "en_US-lessac-medium"
            onnx_path = os.path.join(piper_dir, f"{model_name}.onnx")
            json_path = os.path.join(piper_dir, f"{model_name}.onnx.json")

            def _progress(block, block_size, total):
                if total > 0:
                    pct = min(100, block * block_size * 100 // total)
                    print(f"\r    Progress: {pct}%", end="", flush=True)

            if not os.path.isfile(onnx_path):
                urllib.request.urlretrieve(
                    f"{base_url}/{model_name}.onnx?download=true", onnx_path, _progress
                )
                print()  # newline after progress
            if not os.path.isfile(json_path):
                urllib.request.urlretrieve(
                    f"{base_url}/{model_name}.onnx.json", json_path
                )
            print("  [OK] Piper TTS voice downloaded")
        except Exception as e:
            print(f"\n  [WARN] Could not download Piper voice: {e}")
            print("         Text-to-speech will use fallback (pyttsx3).")
            # Clean up partial downloads
            for p in (onnx_path, json_path):
                try:
                    if os.path.isfile(p) and os.path.getsize(p) < 1000:
                        os.unlink(p)  # Remove corrupted partial download
                except OSError:
                    pass

    print("[OK] Speech models ready\n")


def main():
    # --- Quick flags ---
    if "--update" in sys.argv:
        print("[UPDATE] Pulling latest code and models...")
        subprocess.run(["git", "pull", "--ff-only"], cwd=PROJECT_DIR)
        subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        os.path.join(PROJECT_DIR, "requirements.txt"), "--quiet"])
        # Pull latest Ollama model if configured
        config_file = os.path.join(PROJECT_DIR, "config.json")
        if os.path.exists(config_file):
            with open(config_file, "r") as f:
                cfg = json.load(f)
            model = cfg.get("ollama_model", OLLAMA_DEFAULT_MODEL)
            if _ollama_is_running():
                subprocess.run(["ollama", "pull", model])
        print("[OK] Update complete. Run 'python run.py' to start.")
        sys.exit(0)

    # Auto-relaunch with Python 3.12 if available (needed for PyAudio, faster-whisper)
    _relaunch_with_preferred_python()

    # --- Root logging ---
    import logging
    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(log_dir, "assistant.log"),
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        encoding="utf-8",
    )

    print_banner()

    start_time = time.time()

    print("  [1/8] Checking Python version...")
    check_python()

    print("  [2/8] Checking dependencies...")
    check_dependencies()

    # First-run: interactive config BEFORE model downloads
    # This ensures the user's chosen model gets downloaded, not the default
    print("  [3/8] Checking configuration...")
    is_first_run = _ensure_first_run_setup()
    if not is_first_run:
        print("  [OK] Configuration loaded\n")

    print("  [4/8] Setting up local AI brain...")
    setup_ollama()
    _check_vision_model()

    print("  [5/8] Downloading speech models...")
    _download_speech_models()

    print("  [6/8] Validating AI provider...")
    validate_provider()
    # Cloud provider validation runs in background — continues to next steps

    print("  [7/8] Checking project modules...")
    check_modules()

    # Collect background validation result before launch (may prompt user)
    _finish_provider_validation()

    elapsed = time.time() - start_time
    print(f"  Startup checks completed in {elapsed:.1f}s\n")

    # --- Optional self-test (first run or --selftest flag) ---
    run_test = "--selftest" in sys.argv
    if is_first_run and not run_test:
        try:
            run_test = input("  Run self-test to verify everything works? (y/n): ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            pass
    if run_test:
        print("\n[TEST] Running self-test diagnostics...")
        try:
            from self_test import run_self_test
            report = run_self_test()
            print(report)
            # Save report to logs
            log_dir = os.path.join(PROJECT_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "selftest.log"), "w", encoding="utf-8") as f:
                f.write(report)
            print(f"  Report saved to logs/selftest.log\n")
        except Exception as e:
            print(f"  [WARN] Self-test failed: {e}\n")

    print("  [8/8] Launching assistant...")
    launch()


if __name__ == "__main__":
    main()
