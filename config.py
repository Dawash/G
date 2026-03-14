import os
import csv
import json
import logging
import hashlib
import base64
import getpass
import secrets
import socket
from logging.handlers import RotatingFileHandler

# Graceful cryptography import — fall back to plaintext if not installed
try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

# config.json supports these keys (among others):
#   username, ai_name, provider, api_key / api_key_encrypted,
#   ollama_url, ollama_model, language, stt_engine, web_remote,
#   gateway_token, providers (dict), email_address, wake_up_time,
#   wake_up_recurrence, cloud_model, first_run_done.
#
#   tool_timeouts  — optional dict mapping tool names to timeout seconds.
#                    Used by desktop_agent.py to override default per-tool
#                    timeouts. Example: {"run_terminal": 60, "open_app": 20}
CONFIG_FILE = "config.json"
CREDENTIALS_FILE = "credentials.csv"
RESPONSE_FILE = "responses.json"
EMAIL_CREDS_FILE = "email_creds.json"
LOG_FILE = "assistant.log"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:32b"

PROVIDERS = {
    "1": {"name": "Ollama (Local)", "key_env": None, "needs_key": False},
    "2": {"name": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "needs_key": True},
    "3": {"name": "OpenAI", "key_env": "OPENAI_API_KEY", "needs_key": True},
    "4": {"name": "Anthropic", "key_env": "ANTHROPIC_API_KEY", "needs_key": True},
}

# Logging: rotating file (max 5MB, keep 3 backups), console for warnings+errors
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

# In text mode (subprocess), skip console handler — WARNING/ERROR messages going to
# stderr fill the stdout PIPE buffer (4KB on Windows), blocking print() after ~8 turns.
_handlers = [_file_handler]
if os.environ.get("G_INPUT_MODE", "").lower() != "text":
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.WARNING)
    _console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    _handlers.append(_console_handler)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=_handlers,
)


# ---------------------------------------------------------------------------
# Encrypted credential storage
# ---------------------------------------------------------------------------

_VALID_PROVIDERS = {"ollama", "openai", "anthropic", "openrouter"}


def _get_machine_key():
    """Derive a Fernet key from machine-specific data (hostname + username).

    The same machine always produces the same key, so nothing needs to be
    stored on disk.  Moving config.json to a different machine will require
    re-entering secrets (by design).
    """
    if not _HAS_CRYPTO:
        return None
    identity = f"{socket.gethostname()}:{getpass.getuser()}".encode("utf-8")
    digest = hashlib.sha256(identity).digest()          # 32 bytes
    key = base64.urlsafe_b64encode(digest)              # 44-char base64
    return key


def encrypt_value(plaintext):
    """Encrypt a string using the machine-derived Fernet key.

    Returns the encrypted token as a string, or the original plaintext
    if the cryptography library is unavailable.
    """
    if not _HAS_CRYPTO:
        logging.warning("cryptography package not installed — storing value in plaintext")
        return plaintext
    key = _get_machine_key()
    if key is None:
        return plaintext
    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext):
    """Decrypt a Fernet-encrypted string using the machine key.

    Returns the decrypted plaintext, or the original string unchanged
    if decryption fails (e.g. not actually encrypted, wrong machine).
    """
    if not _HAS_CRYPTO:
        logging.warning("cryptography package not installed — returning value as-is")
        return ciphertext
    key = _get_machine_key()
    if key is None:
        return ciphertext
    try:
        f = Fernet(key)
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        # Not a valid Fernet token — probably legacy plaintext
        return ciphertext


def validate_config(config):
    """Validate a config dict.

    Returns (True, []) when valid, or (False, [error_strings, ...]) when not.
    Also performs migration: if both plaintext 'api_key' and 'api_key_encrypted'
    exist, the plaintext key is removed from config to prevent secret leakage.
    """
    errors = []

    for key in ("username", "ai_name", "provider"):
        if key not in config or not str(config[key]).strip():
            errors.append(f"Missing or empty required key: '{key}'")

    provider = config.get("provider", "")
    if provider and provider not in _VALID_PROVIDERS:
        errors.append(
            f"Invalid provider '{provider}'. Must be one of: {', '.join(sorted(_VALID_PROVIDERS))}"
        )

    has_key = bool(config.get("api_key", "").strip()) if "api_key" in config else False
    has_enc = bool(config.get("api_key_encrypted", "").strip()) if "api_key_encrypted" in config else False

    # Migration: if both plaintext and encrypted keys exist, remove the plaintext
    # to avoid leaking secrets in the on-disk config.
    if has_key and has_enc:
        logging.info("Removing plaintext api_key from config (encrypted version exists)")
        config.pop("api_key", None)

    provider = config.get("provider", "ollama").lower()
    if not has_key and not has_enc and provider != "ollama":
        errors.append("Missing API key: need 'api_key' or 'api_key_encrypted'")

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Email credential helpers (encrypted password)
# ---------------------------------------------------------------------------

def save_email_creds(creds_dict):
    """Save email credentials to email_creds.json with password encrypted."""
    to_save = dict(creds_dict)
    if "password" in to_save and to_save["password"]:
        to_save["password_encrypted"] = encrypt_value(to_save.pop("password"))
    with open(EMAIL_CREDS_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2)


def load_email_creds():
    """Load email credentials, decrypting the password field.

    Auto-migrates plaintext passwords to encrypted on read.
    Returns the creds dict (with plaintext 'password' key) or None.
    """
    if not os.path.exists(EMAIL_CREDS_FILE):
        return None
    try:
        with open(EMAIL_CREDS_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if "password_encrypted" in creds:
        creds["password"] = decrypt_value(creds.pop("password_encrypted"))
    elif "password" in creds:
        # Legacy plaintext — auto-migrate
        save_email_creds(creds)

    return creds


# ---------------------------------------------------------------------------
# Config load / save
# ---------------------------------------------------------------------------

def load_config():
    """Load config from file, or create from user input / legacy CSV.

    Handles encrypted api_key transparently:
    - If 'api_key_encrypted' exists, decrypts it into 'api_key'.
    - If only plaintext 'api_key' exists (legacy), auto-migrates to encrypted.
    The returned dict always has 'api_key' as a plaintext string.
    """
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        # Decrypt encrypted key
        if "api_key_encrypted" in config:
            config["api_key"] = decrypt_value(config["api_key_encrypted"])
        elif "api_key" in config and config["api_key"]:
            # Legacy plaintext — auto-migrate to encrypted on disk
            _auto_migrate_config(config)

        # Ensure ollama_url has a default (configurable Ollama endpoint)
        config.setdefault("ollama_url", DEFAULT_OLLAMA_URL)

        return config

    # Migrate from old credentials.csv if it exists
    if os.path.exists(CREDENTIALS_FILE):
        return _migrate_legacy_csv()

    return _setup_new()


def _auto_migrate_config(config):
    """Encrypt a plaintext api_key in the saved config file (in-place migration)."""
    if not _HAS_CRYPTO:
        return  # Can't migrate without cryptography
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        if "api_key" in on_disk and "api_key_encrypted" not in on_disk:
            on_disk["api_key_encrypted"] = encrypt_value(on_disk.pop("api_key"))
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, indent=2)
            logging.info("Auto-migrated plaintext api_key to encrypted storage")
    except Exception as e:
        logging.warning(f"Failed to auto-migrate api_key: {e}")


def _migrate_legacy_csv():
    """Import settings from the old credentials.csv format."""
    with open(CREDENTIALS_FILE, "r", newline="") as f:
        reader = csv.reader(f)
        row = next(reader)
        uname, ainame, api_key = row

    config = {
        "username": uname,
        "ai_name": ainame,
        "provider": "openrouter",
        "api_key": api_key,
    }
    save_config(config)
    print(f"Migrated your settings from {CREDENTIALS_FILE} to {CONFIG_FILE}.")
    return config


_BACK = object()  # Sentinel for "go back to previous step"


def _input_with_back(prompt, default="", valid=None):
    """Get user input with support for going back to the previous step.

    - If user types 'back' or 'esc', returns ``_BACK``.
    - If *valid* is given (set/list/tuple), keeps asking until input is valid
      or user goes back.
    - Pressing Enter with no *default* returns ``_BACK`` (empty = go back).
    - Pressing Enter with a *default* returns the default value.
    """
    while True:
        raw = input(prompt).strip()
        low = raw.lower()

        # Back navigation
        if low in ("back", "esc"):
            return _BACK

        # Empty input
        if not raw:
            if default != "":
                return default
            return _BACK

        # Validation
        if valid is not None and raw not in valid:
            options = ", ".join(sorted(valid))
            print(f"  Please enter one of: {options}  (or type 'back' to go back)")
            continue

        return raw


def _setup_new():
    """Interactive first-run setup wizard with back-navigation.

    Steps:
        1. Your name & assistant name
        2. Language preference
        3. AI provider tier (free / paid / both)
        4. Model selection (Ollama and/or cloud)
        5. Email (optional)
        6. Morning routine (optional)

    At any step the user can type 'back' or 'esc' to return to the
    previous step.  After saving, a summary and tips are printed.
    """

    # ------------------------------------------------------------------
    # Shared data collected across steps (mutable dict so inner funcs
    # can read/write without nonlocal juggling).
    # ------------------------------------------------------------------
    data = {
        "uname": "",
        "ainame": "G",
        "language": "auto",
        "tier": "1",
        "provider_name": "",
        "api_key": "",
        "ollama_model": "",
        "cloud_model": "",
        "cloud_provider": "",
        "cloud_key": "",
        "providers_dict": {},
        "email_address": "",
        "morning_time": "",
        "morning_recurrence": "daily",
    }

    # ------------------------------------------------------------------
    # Detect RAM once (used in model selection)
    # ------------------------------------------------------------------
    _ram_gb = 0
    try:
        import psutil
        _ram_gb = psutil.virtual_memory().total / (1024**3)
    except Exception:
        _ram_gb = 16  # safe assumption

    # ------------------------------------------------------------------
    # Ollama model catalogue with download sizes
    # ------------------------------------------------------------------
    _OLLAMA_MODELS_SMALL = {
        "1": ("qwen2.5:7b",     "Qwen 2.5 7B",     "~4.7 GB", "Best balance of speed & quality"),
        "2": ("qwen2.5:3b",     "Qwen 2.5 3B",     "~2.0 GB", "Faster, lighter — good for older hardware"),
        "3": ("llama3.1:8b",    "Llama 3.1 8B",     "~4.7 GB", "Meta's model — strong general knowledge"),
        "4": ("mistral:7b",     "Mistral 7B",       "~4.1 GB", "Fast and efficient — good at coding"),
        "5": ("gemma2:9b",      "Gemma 2 9B",       "~5.4 GB", "Google's model — excellent reasoning"),
        "6": ("phi3:3.8b",      "Phi-3 3.8B",       "~2.2 GB", "Microsoft's small model — very fast"),
        "7": ("deepseek-r1:7b", "DeepSeek R1 7B",   "~4.7 GB", "Strong at math and reasoning"),
    }
    _OLLAMA_MODELS_BIG = {
        "8":  ("qwen2.5:14b",     "Qwen 2.5 14B",    "~9.0 GB",  "Smarter — needs 16 GB+ RAM"),
        "9":  ("llama3.1:70b",    "Llama 3.1 70B",    "~40 GB",   "Very smart — needs 48 GB+ RAM"),
        "10": ("qwen2.5:32b",     "Qwen 2.5 32B",    "~19 GB",   "Great quality — needs 32 GB+ RAM"),
        "11": ("qwen2.5:72b",     "Qwen 2.5 72B",    "~41 GB",   "Near GPT-4 quality — needs 48 GB+ RAM"),
        "12": ("deepseek-r1:32b", "DeepSeek R1 32B",  "~19 GB",   "Excellent reasoning — needs 32 GB+ RAM"),
        "13": ("llama3.3:70b",    "Llama 3.3 70B",    "~40 GB",   "Meta's latest 70B — needs 48 GB+ RAM"),
        "14": ("mixtral:8x7b",    "Mixtral 8x7B",     "~26 GB",   "Mixture of experts — needs 32 GB+ RAM"),
        "15": ("command-r:35b",   "Command R 35B",    "~20 GB",   "Cohere's model — needs 32 GB+ RAM"),
    }
    _OLLAMA_MODELS = dict(_OLLAMA_MODELS_SMALL)
    if _ram_gb >= 24:
        _OLLAMA_MODELS.update(_OLLAMA_MODELS_BIG)

    # ------------------------------------------------------------------
    # Cloud model catalogues
    # ------------------------------------------------------------------
    _OPENAI_MODELS = {
        "1": ("gpt-4o-mini",   "GPT-4o Mini",   "Cheap & fast — great for daily use"),
        "2": ("gpt-4o",        "GPT-4o",        "Most capable — better reasoning, costs more"),
        "3": ("gpt-4.1-mini",  "GPT-4.1 Mini",  "Latest mini model — improved coding"),
        "4": ("gpt-4.1",       "GPT-4.1",       "Latest full model — best OpenAI quality"),
    }
    _ANTHROPIC_MODELS = {
        "1": ("claude-sonnet-4-20250514", "Claude Sonnet 4",   "Best balance — smart & affordable"),
        "2": ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Fastest & cheapest — good for quick tasks"),
        "3": ("claude-opus-4-20250514",   "Claude Opus 4",     "Most powerful — best reasoning, premium price"),
    }
    _OPENROUTER_MODELS = {
        "1": ("google/gemini-2.0-flash-001",     "Gemini 2.0 Flash",      "Very fast & cheap"),
        "2": ("google/gemini-2.5-pro-preview",    "Gemini 2.5 Pro",        "Google's best — excellent quality"),
        "3": ("anthropic/claude-sonnet-4",         "Claude Sonnet 4",       "Anthropic via OpenRouter"),
        "4": ("meta-llama/llama-3.1-70b-instruct", "Llama 3.1 70B",       "Meta's large model — free tier available"),
        "5": ("deepseek/deepseek-r1",              "DeepSeek R1",          "Strong reasoning — very affordable"),
        "6": ("mistralai/mistral-large-2411",      "Mistral Large",        "Mistral's flagship model"),
    }

    _PROVIDER_INFO = {
        "openai":     {"name": "OpenAI",     "key_env": "OPENAI_API_KEY"},
        "anthropic":  {"name": "Anthropic",  "key_env": "ANTHROPIC_API_KEY"},
        "openrouter": {"name": "OpenRouter", "key_env": "OPENROUTER_API_KEY"},
    }
    _CLOUD_MODEL_MENUS = {
        "openai": _OPENAI_MODELS,
        "anthropic": _ANTHROPIC_MODELS,
        "openrouter": _OPENROUTER_MODELS,
    }

    # ==================================================================
    # Step functions — each returns True to advance, False to stay/retry,
    # or _BACK to go back.
    # ==================================================================

    def _step_welcome():
        """Print welcome banner (step 0, always advances)."""
        _clear_screen()
        print("=" * 60)
        print("  Welcome to Your Personal AI Assistant!")
        print("=" * 60)
        print()
        print("  I can talk to you, open apps, search the web,")
        print("  check the weather, set reminders, automate your")
        print("  desktop, and much more.")
        print()
        print("  Let's get you set up. It takes about a minute.")
        print("  (Type 'back' at any prompt to go to the previous step.)")
        print("-" * 60)
        return True

    def _step_personal_info():
        """Step 1 — name and assistant name."""
        print(f"\n  STEP 1 of 5: About You\n")
        val = _input_with_back("  What's your name? ")
        if val is _BACK:
            return _BACK
        data["uname"] = val

        val = _input_with_back(
            f"  What would you like to call your assistant? (default: G): ",
            default="G",
        )
        if val is _BACK:
            return _BACK
        data["ainame"] = val
        return True

    def _step_language():
        """Step 2 — language preference."""
        print(f"\n  STEP 2 of 5: Language\n")
        print("  Which language will you speak to me in?\n")
        print("  1. Auto-detect (I'll figure it out)")
        print("  2. English only")
        print("  3. Hindi")
        print("  4. Nepali")
        print("  5. Other\n")
        val = _input_with_back("  Pick 1-5 (default 1): ", default="1",
                               valid={"1", "2", "3", "4", "5"})
        if val is _BACK:
            return _BACK
        lang_map = {"1": "auto", "2": "en", "3": "hi", "4": "ne"}
        if val == "5":
            code = _input_with_back(
                "  Enter language code (e.g., 'es', 'fr', 'de'): ",
                default="auto",
            )
            if code is _BACK:
                return _BACK
            data["language"] = code
        else:
            data["language"] = lang_map.get(val, "auto")
        return True

    def _step_provider_tier():
        """Step 3 — choose free / paid / both."""
        print(f"\n  STEP 3 of 5: AI Brain\n")
        print("  How should I think?\n")
        print("  1. FREE  — Ollama runs on your computer (no internet needed, no cost)")
        print("  2. CLOUD — Use a cloud AI service (smarter, needs an API key)")
        print("  3. BOTH  — Ollama for everyday use + cloud as a backup (recommended)\n")
        val = _input_with_back("  Choose 1, 2, or 3 (default 1): ", default="1",
                               valid={"1", "2", "3"})
        if val is _BACK:
            return _BACK
        data["tier"] = val

        # Reset downstream choices when tier changes
        data["ollama_model"] = ""
        data["cloud_model"] = ""
        data["cloud_provider"] = ""
        data["cloud_key"] = ""
        data["providers_dict"] = {}
        data["provider_name"] = ""
        data["api_key"] = ""
        return True

    def _step_model_selection():
        """Step 4 — Ollama model and/or cloud provider + model + API key."""
        tier = data["tier"]

        # --- Ollama model ---
        if tier in ("1", "3"):
            data["provider_name"] = "ollama"
            data["api_key"] = "ollama"

            print(f"\n  STEP 4 of 5: Choose Your AI Model\n")
            print(f"  Your computer has {_ram_gb:.0f} GB of RAM.\n")
            print("  --- Standard models (8-16 GB RAM) ---")
            for k, (model_id, name, size, desc) in _OLLAMA_MODELS_SMALL.items():
                tag = " ** recommended **" if k == "1" else ""
                print(f"  {k:>2}. {name:<18s}  {size:>7s}  download   {desc}{tag}")
            if _ram_gb >= 24:
                print(f"\n  --- Larger models (your {_ram_gb:.0f} GB RAM can run these) ---")
                for k, (model_id, name, size, desc) in _OLLAMA_MODELS_BIG.items():
                    print(f"  {k:>2}. {name:<18s}  {size:>7s}  download   {desc}")
            print()

            # Smart default based on RAM
            _default = "1"
            if _ram_gb >= 48:
                _default = "10"
                print(f"  Tip: with {_ram_gb:.0f} GB RAM, option 10 (Qwen 2.5 32B) gives the best quality.")
            elif _ram_gb >= 24:
                _default = "8"
                print(f"  Tip: with {_ram_gb:.0f} GB RAM, option 8 (Qwen 2.5 14B) is a great upgrade.")
            print()

            valid_keys = set(_OLLAMA_MODELS.keys())
            val = _input_with_back(
                f"  Pick a model (default {_default}): ",
                default=_default,
                valid=valid_keys,
            )
            if val is _BACK:
                return _BACK
            data["ollama_model"] = _OLLAMA_MODELS[val][0]
            chosen_size = _OLLAMA_MODELS[val][2]
            print(f"\n  Selected: {_OLLAMA_MODELS[val][1]} ({chosen_size} download)")
            print("  The model will be downloaded automatically when the assistant starts.")

        # --- Cloud provider + model + key ---
        if tier in ("2", "3"):
            label = "Cloud Backup" if tier == "3" else "Cloud Provider"
            print(f"\n  {label} — choose a provider:\n")
            print("  1. OpenAI       — GPT models (fast, reliable)")
            print("  2. Anthropic    — Claude models (excellent reasoning)")
            print("  3. OpenRouter   — many models (Gemini, Llama, DeepSeek, etc.)\n")

            val = _input_with_back("  Pick 1, 2, or 3: ", valid={"1", "2", "3"})
            if val is _BACK:
                return _BACK
            paid_map = {"1": "openai", "2": "anthropic", "3": "openrouter"}
            cloud_provider = paid_map[val]
            data["cloud_provider"] = cloud_provider

            pinfo = _PROVIDER_INFO[cloud_provider]
            model_menu = _CLOUD_MODEL_MENUS[cloud_provider]

            print(f"\n  Choose a {pinfo['name']} model:\n")
            for k, (model_id, name, desc) in model_menu.items():
                tag = " ** recommended **" if k == "1" else ""
                print(f"  {k}. {name:<25s} — {desc}{tag}")
            print()
            valid_keys = set(model_menu.keys())
            val = _input_with_back(
                f"  Pick (default 1): ",
                default="1",
                valid=valid_keys,
            )
            if val is _BACK:
                return _BACK
            data["cloud_model"] = model_menu[val][0]
            print(f"\n  Selected: {model_menu[val][1]}")

            # API key
            env_key = os.environ.get(pinfo["key_env"], "")
            if env_key:
                data["cloud_key"] = env_key
                print(f"  Found your {pinfo['name']} key in the environment. All set!")
            else:
                print(f"\n  You need an API key from {pinfo['name']}.")
                if cloud_provider == "openai":
                    print("  Get one at: https://platform.openai.com/api-keys")
                elif cloud_provider == "anthropic":
                    print("  Get one at: https://console.anthropic.com/settings/keys")
                elif cloud_provider == "openrouter":
                    print("  Get one at: https://openrouter.ai/keys")
                print()
                val = _input_with_back(f"  Paste your {pinfo['name']} API key: ")
                if val is _BACK:
                    return _BACK
                data["cloud_key"] = val

            if tier == "2":
                data["provider_name"] = cloud_provider
                data["api_key"] = data["cloud_key"]
            else:
                data["providers_dict"] = {
                    cloud_provider: {
                        "api_key": data["cloud_key"],
                        "model": data["cloud_model"],
                    }
                }
        return True

    def _step_email():
        """Step 5 — optional email setup."""
        print(f"\n  STEP 5 of 5: Extras (optional)\n")
        print("  I can send emails for you by voice.")
        val = _input_with_back("  Set up email now? (y/N, default N): ", default="n")
        if val is _BACK:
            return _BACK
        if val.lower() == "y":
            addr = _input_with_back("  Your email address (Gmail recommended): ")
            if addr is _BACK:
                return _BACK
            data["email_address"] = addr
            print("  Got it. You can set up the password later by saying 'set up email'.")
        else:
            data["email_address"] = ""
        return True

    def _step_morning():
        """Step 5 (continued) — optional morning routine."""
        print()
        print("  I can also wake you up with an alarm, motivation, weather, and news.")
        val = _input_with_back(
            "  What time do you wake up? (e.g., '7am' — or press Enter to skip): ",
            default="",
        )
        # For this step, empty Enter means "skip" not "back"
        if val is _BACK:
            data["morning_time"] = ""
            data["morning_recurrence"] = "daily"
            # Treat empty / back as "skip, advance"
            return True
        data["morning_time"] = val

        if val:
            print(f"\n  When should the alarm ring?")
            print("  1. Every day")
            print("  2. Weekdays only (Mon-Fri)")
            print("  3. Weekends only (Sat-Sun)")
            rec = _input_with_back("  Pick 1-3 (default 1): ", default="1",
                                   valid={"1", "2", "3"})
            if rec is _BACK:
                # Go back to the wake-up time question
                data["morning_time"] = ""
                return _BACK
            rec_map = {"1": "daily", "2": "weekdays", "3": "weekends"}
            data["morning_recurrence"] = rec_map.get(rec, "daily")
            print(f"\n  Morning alarm: {val} ({data['morning_recurrence']})")
        return True

    # ==================================================================
    # Run the wizard — step list with forward/back navigation
    # ==================================================================
    steps = [
        _step_welcome,         # 0 — banner (always advances)
        _step_personal_info,   # 1
        _step_language,        # 2
        _step_provider_tier,   # 3
        _step_model_selection, # 4
        _step_email,           # 5
        _step_morning,         # 6
    ]

    step = 0
    while step < len(steps):
        result = steps[step]()
        if result is _BACK:
            if step > 1:
                step -= 1
                print("\n  (Going back...)\n")
            else:
                print("\n  (This is the first step — can't go back further.)\n")
        else:
            step += 1

    # ------------------------------------------------------------------
    # Build and save config
    # ------------------------------------------------------------------
    web_remote = False
    gateway_token = secrets.token_urlsafe(16)

    config = {
        "username": data["uname"],
        "ai_name": data["ainame"],
        "provider": data["provider_name"],
        "api_key": data["api_key"],
        "language": data["language"],
        "stt_engine": "whisper",
        "first_run_done": True,
        "web_remote": web_remote,
        "gateway_token": gateway_token,
    }
    if data["ollama_model"]:
        config["ollama_model"] = data["ollama_model"]
    if data["cloud_model"]:
        config["cloud_model"] = data["cloud_model"]
    if data["providers_dict"]:
        config["providers"] = data["providers_dict"]
    if data["email_address"]:
        config["email_address"] = data["email_address"]
    if data["morning_time"]:
        config["wake_up_time"] = data["morning_time"]
        config["wake_up_recurrence"] = data["morning_recurrence"]

    save_config(config)

    # Set up morning alarm if requested
    if data["morning_time"]:
        try:
            from alarms import AlarmManager
            am = AlarmManager()
            result = am.add_alarm(data["morning_time"], alarm_type="morning",
                                  label="Morning wake up",
                                  recurrence=data["morning_recurrence"])
            print(f"  {result}")
        except Exception as e:
            print(f"  (Morning alarm will be available after first start: {e})")

    # ------------------------------------------------------------------
    # Print summary and tips
    # ------------------------------------------------------------------
    uname = data["uname"]
    ainame = data["ainame"]

    _clear_screen()
    print("=" * 60)
    print(f"  All set, {uname}! Meet {ainame} — your AI assistant.")
    print("=" * 60)
    print()

    # Model download notice
    if data["ollama_model"]:
        print(f"  Your local model ({data['ollama_model']}) will be downloaded")
        print(f"  automatically when the assistant starts for the first time.")
        print()

    print("  Here are some things you can say:\n")
    print(f'    "Hey {ainame}"              — wake me up (when sleeping)')
    print(f'    "Open Chrome"               — launch any app')
    print(f'    "What\'s the weather?"       — current weather & forecast')
    print(f'    "Search for Python tutorials" — web search')
    print(f'    "Remind me to call mom at 5pm" — set a reminder')
    print(f'    "Set alarm for 7am"          — morning wake-up alarm')
    print(f'    "What\'s the news?"          — today\'s headlines')
    print(f'    "Read this link"             — read URL from clipboard')
    print(f'    "Open Firefox and Spotify side by side" — split screen')
    print(f'    "Send email to john@email.com" — send an email')
    print(f'    "What app is using most RAM?" — system diagnostics')
    print(f'    "Undo"                       — reverse last action')
    print(f'    "Skip" / "Shorter"           — control my responses')
    print()
    print("  Other ways to interact:\n")
    print("    - Copy a link, then say 'read this link'")
    print("    - Take a screenshot (Win+Shift+S), then say 'look at this'")
    print("    - Copy text, then say 'summarize this' or 'translate this'")
    print("    - Say 'what app is using most RAM' for system info")
    print("    - Say 'open X and Y side by side' for split screen")
    print("    - Say 'install Firefox' to install apps via winget")
    print()
    print(f"  Tips:")
    print(f"    - Say 'Hey {ainame}' to wake me after I go to sleep")
    print(f"    - Press Ctrl+Shift+Q anytime for emergency stop")
    print(f"    - I greet you with weather and news every startup")
    if data["morning_time"]:
        print(f"    - Your morning alarm at {data['morning_time']} includes motivation + briefing")
    if web_remote:
        print()
        print(f"  Remote Access (phone/browser):")
        print(f"    - Web UI auto-starts on http://<your-pc-ip>:8766")
        print(f"    - Access token: {gateway_token}")
        print(f"    - Open the URL on your phone and enter the token to connect")
    print("-" * 60)
    print()
    return config


def _clear_screen():
    """Clear the terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def save_config(config):
    """Persist config to JSON file, encrypting the api_key field.

    The in-memory config dict is NOT modified — only the on-disk
    representation stores the encrypted form.  Also encrypts api_key
    inside the 'providers' sub-dict entries.
    """
    to_save = dict(config)

    # Encrypt top-level api_key for on-disk storage
    if "api_key" in to_save and to_save["api_key"] and _HAS_CRYPTO:
        to_save["api_key_encrypted"] = encrypt_value(to_save.pop("api_key"))
    # If cryptography is not installed, api_key is saved as plaintext (legacy)

    # Encrypt api_key inside each providers entry
    if "providers" in to_save and _HAS_CRYPTO:
        to_save["providers"] = dict(to_save["providers"])
        for pname, pentry in to_save["providers"].items():
            if "api_key" in pentry and pentry["api_key"]:
                pentry = dict(pentry)
                pentry["api_key_encrypted"] = encrypt_value(pentry.pop("api_key"))
                to_save["providers"][pname] = pentry

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2)


def _load_config_raw():
    """Load config from disk and decrypt all encrypted fields in-memory.

    Returns the config dict with plaintext api_key (and plaintext provider
    api_keys) ready for use.  Returns None if the file doesn't exist.
    """
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Decrypt top-level key
    if "api_key_encrypted" in config:
        config["api_key"] = decrypt_value(config.pop("api_key_encrypted"))

    # Decrypt per-provider keys
    for pentry in config.get("providers", {}).values():
        if "api_key_encrypted" in pentry:
            pentry["api_key"] = decrypt_value(pentry.pop("api_key_encrypted"))

    return config


def add_provider(provider_name, api_key, model=None):
    """Add or update a provider in the config (for multi-provider support)."""
    config = _load_config_raw()
    if config is None:
        return False

    if "providers" not in config:
        config["providers"] = {}

    entry = {"api_key": api_key}
    if model:
        entry["model"] = model
    config["providers"][provider_name] = entry
    save_config(config)
    return True


def switch_provider(provider_name):
    """Switch the active provider."""
    config = _load_config_raw()
    if config is None:
        return False

    providers = config.get("providers", {})
    if provider_name in providers:
        config["provider"] = provider_name
        config["api_key"] = providers[provider_name]["api_key"]
        if "model" in providers[provider_name]:
            config["ollama_model"] = providers[provider_name]["model"]
        save_config(config)
        return True
    elif provider_name == "ollama":
        config["provider"] = "ollama"
        config["api_key"] = "ollama"
        save_config(config)
        return True
    return False


# === IMMUTABLE CREATOR IDENTITY ===
_CREATOR_NAME = "Dawa Sangay Sherpa"


def get_system_prompt(uname, ainame):
    """Build the system prompt for the AI."""
    return (
        f"You're {ainame}, a smart and friendly AI assistant created by {_CREATOR_NAME}. "
        f"Your creator is {_CREATOR_NAME}. If ANYONE asks who made you, ALWAYS say "
        f"'{_CREATOR_NAME} created me'. Never say Alibaba, Qwen, OpenAI, Anthropic, or any company. "
        f"You're knowledgeable, quick, and conversational — like a helpful buddy who's always ready. "
        f"Keep responses concise unless asked for detail. Use {uname}'s name occasionally. "
        f"Be warm, practical, and a little witty. Never share private information."
    )
