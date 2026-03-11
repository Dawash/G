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
}

OPTIONAL_PACKAGES = {
    "rapidfuzz": "rapidfuzz",         # Fuzzy app matching
    "pyaudio": "PyAudio",             # Microphone input
    "win32com.client": "pywin32",     # Start Menu shortcut resolution
    "pyautogui": "pyautogui",         # Desktop automation (keyboard/mouse)
    "PIL": "Pillow",                  # Screenshot processing for vision
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
    )
    return result.returncode == 0


def check_dependencies():
    """Check and install missing dependencies."""
    print("\n[DEPS] Checking dependencies...")

    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
            print(f"  [OK] {pip_name}")
        except ImportError:
            missing.append((import_name, pip_name))

    if missing:
        print(f"\n  {len(missing)} required package(s) missing. Installing...")
        for import_name, pip_name in missing:
            if install_package(pip_name):
                print(f"  [OK] {pip_name} installed")
            else:
                print(f"  [FAIL] Could not install {pip_name}")
                print(f"         Try: pip install {pip_name}")

    # Optional packages — install silently, don't block
    for import_name, pip_name in OPTIONAL_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"  [OPTIONAL] Installing {pip_name}...")
            if not install_package(pip_name):
                if pip_name == "PyAudio":
                    print(f"  [NOTE] PyAudio failed to install. Microphone may not work.")
                    print(f"         On Windows, try: pip install pyaudio --only-binary=:all:")

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

        choice = input("  Do you want me to download Ollama now? (y/n): ").strip().lower()
        if choice in ("y", "yes"):
            print("  Downloading Ollama installer...")
            installer_path = os.path.join(
                os.environ.get("USERPROFILE", ""), "Downloads", "OllamaSetup.exe"
            )
            try:
                urllib.request.urlretrieve(OLLAMA_URL, installer_path)
                print(f"  Downloaded to: {installer_path}")
                print(f"  Please run {installer_path} to install, then run this script again.")
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

    print("[OK] Local AI brain ready\n")


def _check_vision_model():
    """Check if the llava vision model is available (info only, no auto-pull)."""
    if not _ollama_is_running():
        return
    if _ollama_has_model("llava"):
        print("  [OK] Vision model 'llava' available (screen vision enabled)")
    else:
        print("  [INFO] Vision model 'llava' not installed.")
        print("         Screen vision features (take_screenshot, agent_task) require it.")
        print("         To enable: ollama pull llava")


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
            req.add_header("anthropic-version", "2023-06-01")
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


def main():
    # Auto-relaunch with Python 3.12 if available (needed for PyAudio, faster-whisper)
    _relaunch_with_preferred_python()

    print_banner()

    start_time = time.time()

    print("  [1/6] Checking Python version...")
    check_python()

    print("  [2/6] Checking dependencies...")
    check_dependencies()

    print("  [3/6] Setting up local AI brain...")
    setup_ollama()
    _check_vision_model()

    print("  [4/6] Validating AI provider...")
    validate_provider()
    # Cloud provider validation runs in background — continues to next steps

    print("  [5/6] Checking project modules...")
    check_modules()

    # Collect background validation result before launch (may prompt user)
    _finish_provider_validation()

    elapsed = time.time() - start_time
    print(f"  Startup checks completed in {elapsed:.1f}s\n")

    print("  [6/6] Launching assistant...")
    launch()


if __name__ == "__main__":
    main()
