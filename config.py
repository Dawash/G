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

CONFIG_FILE = "config.json"
CREDENTIALS_FILE = "credentials.csv"
RESPONSE_FILE = "responses.json"
EMAIL_CREDS_FILE = "email_creds.json"
LOG_FILE = "assistant.log"

DEFAULT_OLLAMA_URL = "http://localhost:11434"

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

    if not has_key and not has_enc:
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


def _setup_new():
    """Interactive first-run setup with introduction and guided onboarding."""
    _clear_screen()
    print("=" * 60)
    print("  Welcome to Your Personal AI Operating System!")
    print("=" * 60)
    print()
    print("  I'm an AI assistant that can:")
    print("    - Speak naturally and understand your voice")
    print("    - Open apps, search the web, control your desktop")
    print("    - Check weather, news, set reminders and alarms")
    print("    - Read web pages, manage files, send emails")
    print("    - Automate complex multi-step tasks on screen")
    print("    - Remember your preferences and learn over time")
    print()
    print("  Let's get you set up! This takes about 1 minute.")
    print("-" * 60)

    # --- Step 1: Personal info ---
    print("\n  STEP 1: About You\n")
    uname = ""
    while not uname:
        uname = input("  What's your name? ").strip()
    ainame = input(f"  What would you like to call me? (e.g., Jarvis, Nova — default: G): ").strip() or "G"

    # --- Step 2: Language preference ---
    print(f"\n  STEP 2: Language\n")
    print("  1. Auto-detect (I'll figure it out from your speech)")
    print("  2. English only")
    print("  3. Hindi")
    print("  4. Nepali")
    print("  5. Other (type language code)\n")
    lang_choice = input("  Pick (1-5, default 1): ").strip() or "1"
    lang_map = {"1": "auto", "2": "en", "3": "hi", "4": "ne"}
    language = lang_map.get(lang_choice, "auto")
    if lang_choice == "5":
        language = input("  Enter language code (e.g., 'es', 'fr', 'de'): ").strip() or "auto"

    # --- Step 3: AI provider ---
    print(f"\n  STEP 3: AI Brain\n")
    print("  1. FREE — Ollama (runs locally, no internet needed, no cost)")
    print("  2. PAID — Cloud AI (smarter, needs API key + internet)")
    print("  3. BOTH — Ollama primary + cloud backup (recommended)\n")

    tier = input("  Choose (1/2/3, default 1): ").strip() or "1"
    while tier not in ("1", "2", "3"):
        tier = input("  Please pick 1, 2, or 3: ").strip()

    api_key = ""
    ollama_model = ""
    cloud_model = ""
    provider_name = ""
    providers_dict = {}

    # --- FREE model selection (Ollama) ---
    _OLLAMA_MODELS = {
        "1": ("qwen2.5:7b",    "Qwen 2.5 7B",    "Best balance of speed & quality (recommended)"),
        "2": ("qwen2.5:3b",    "Qwen 2.5 3B",    "Faster, lighter — good for older hardware"),
        "3": ("llama3.1:8b",   "Llama 3.1 8B",   "Meta's model — strong general knowledge"),
        "4": ("mistral:7b",    "Mistral 7B",      "Fast and efficient — good at coding"),
        "5": ("gemma2:9b",     "Gemma 2 9B",      "Google's model — excellent reasoning"),
        "6": ("phi3:3.8b",     "Phi-3 3.8B",      "Microsoft's small model — very fast"),
        "7": ("deepseek-r1:7b", "DeepSeek R1 7B", "Strong at math and reasoning"),
        "8": ("qwen2.5:14b",   "Qwen 2.5 14B",   "Larger model — smarter but needs 16GB+ RAM"),
    }

    if tier in ("1", "3"):
        provider_name = "ollama"
        api_key = "ollama"

        print(f"\n  Choose a free local model:\n")
        for k, (model_id, name, desc) in _OLLAMA_MODELS.items():
            rec = " (recommended)" if k == "1" else ""
            print(f"  {k}. {name:<20s} — {desc}{rec}")
        print()
        ollama_choice = input("  Pick (1-8, default 1): ").strip() or "1"
        if ollama_choice in _OLLAMA_MODELS:
            ollama_model = _OLLAMA_MODELS[ollama_choice][0]
        else:
            ollama_model = "qwen2.5:7b"
        print(f"\n  Using {ollama_model} (free, local)")
        print("  Make sure Ollama is installed and running (ollama serve)")

    # --- PAID model selection (Cloud) ---
    _OPENAI_MODELS = {
        "1": ("gpt-4o-mini",   "GPT-4o Mini",   "Cheap & fast — great for daily use (recommended)"),
        "2": ("gpt-4o",        "GPT-4o",        "Most capable — better reasoning, costs more"),
        "3": ("gpt-4.1-mini",  "GPT-4.1 Mini",  "Latest mini model — improved coding"),
        "4": ("gpt-4.1",       "GPT-4.1",       "Latest full model — best OpenAI quality"),
    }
    _ANTHROPIC_MODELS = {
        "1": ("claude-sonnet-4-20250514", "Claude Sonnet 4",   "Best balance — smart & affordable (recommended)"),
        "2": ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Fastest & cheapest — good for quick tasks"),
        "3": ("claude-opus-4-20250514",   "Claude Opus 4",     "Most powerful — best reasoning, premium price"),
    }
    _OPENROUTER_MODELS = {
        "1": ("google/gemini-2.0-flash-001",     "Gemini 2.0 Flash",      "Very fast & cheap (recommended)"),
        "2": ("google/gemini-2.5-pro-preview",    "Gemini 2.5 Pro",        "Google's best — excellent quality"),
        "3": ("anthropic/claude-sonnet-4",         "Claude Sonnet 4",       "Anthropic via OpenRouter"),
        "4": ("meta-llama/llama-3.1-70b-instruct", "Llama 3.1 70B",       "Meta's large model — free tier available"),
        "5": ("deepseek/deepseek-r1",              "DeepSeek R1",          "Strong reasoning — very affordable"),
        "6": ("mistralai/mistral-large-2411",      "Mistral Large",        "Mistral's flagship model"),
    }

    if tier in ("2", "3"):
        print("\n  Which cloud provider?\n")
        print("  1. OpenAI       (GPT models — fast, reliable)")
        print("  2. Anthropic    (Claude models — excellent reasoning)")
        print("  3. OpenRouter   (many models — Gemini, Llama, DeepSeek, etc.)\n")

        paid_choice = input("  Pick (1/2/3): ").strip()
        while paid_choice not in ("1", "2", "3"):
            paid_choice = input("  Please pick 1, 2, or 3: ").strip()

        paid_map = {"1": "openai", "2": "anthropic", "3": "openrouter"}
        cloud_provider = paid_map[paid_choice]
        provider_info = {
            "openai": {"name": "OpenAI", "key_env": "OPENAI_API_KEY"},
            "anthropic": {"name": "Anthropic", "key_env": "ANTHROPIC_API_KEY"},
            "openrouter": {"name": "OpenRouter", "key_env": "OPENROUTER_API_KEY"},
        }[cloud_provider]

        # Model selection for the chosen provider
        model_menu = {
            "openai": _OPENAI_MODELS,
            "anthropic": _ANTHROPIC_MODELS,
            "openrouter": _OPENROUTER_MODELS,
        }[cloud_provider]

        print(f"\n  Choose a {provider_info['name']} model:\n")
        for k, (model_id, name, desc) in model_menu.items():
            rec = " (recommended)" if k == "1" else ""
            print(f"  {k}. {name:<25s} — {desc}{rec}")
        print()
        model_choice = input(f"  Pick (1-{len(model_menu)}, default 1): ").strip() or "1"
        if model_choice in model_menu:
            cloud_model = model_menu[model_choice][0]
        else:
            cloud_model = model_menu["1"][0]
        print(f"\n  Using {cloud_model}")

        # API key
        env_key = os.environ.get(provider_info["key_env"], "")
        if env_key:
            cloud_key = env_key
            print(f"  Found {provider_info['name']} key in environment variable.")
        else:
            cloud_key = input(f"  Enter your {provider_info['name']} API key: ").strip()

        if tier == "2":
            provider_name = cloud_provider
            api_key = cloud_key
        else:
            # Both: Ollama primary, cloud as backup provider
            providers_dict[cloud_provider] = {"api_key": cloud_key, "model": cloud_model}

    # --- Step 4: Optional email ---
    print(f"\n  STEP 4: Email (optional — for sending emails via voice)\n")
    email_setup = input("  Set up email now? (y/N): ").strip().lower()
    email_address = ""
    if email_setup == "y":
        email_address = input("  Your email address (Gmail recommended): ").strip()
        if email_address:
            print("  You can configure the password later by saying 'set up email'.")

    # --- Step 5: Morning routine ---
    print(f"\n  STEP 5: Morning Routine\n")
    print("  I can wake you up every morning with an alarm sound,")
    print("  a motivational message, weather forecast, and news summary!")
    print()
    morning_time = input("  What time do you usually wake up? (e.g., '7am', '6:30 AM', or press Enter to skip): ").strip()
    morning_recurrence = "daily"
    if morning_time:
        print(f"\n  When should the alarm ring?")
        print("  1. Every day")
        print("  2. Weekdays only (Mon-Fri)")
        print("  3. Weekends only (Sat-Sun)")
        rec_choice = input("  Pick (1/2/3, default 1): ").strip() or "1"
        rec_map = {"1": "daily", "2": "weekdays", "3": "weekends"}
        morning_recurrence = rec_map.get(rec_choice, "daily")
        print(f"\n  Morning alarm set for {morning_time} ({morning_recurrence})!")
        print("  I'll play an alarm sound, then give you motivation + weather + news.")

    # --- Step 6: Web Remote Access ---
    print(f"\n  STEP 6: Web Remote Access (optional)\n")
    print("  Control the assistant from your phone or any browser.")
    print("  Opens a web dashboard you can access over your local network.\n")
    web_remote_choice = input("  Enable web remote access? (y/N): ").strip().lower()
    web_remote = web_remote_choice == "y"
    gateway_token = ""
    if web_remote:
        gateway_token = secrets.token_urlsafe(16)
        print(f"  Web remote enabled! Token: {gateway_token}")

    # --- Build config ---
    config = {
        "username": uname,
        "ai_name": ainame,
        "provider": provider_name,
        "api_key": api_key,
        "language": language,
        "stt_engine": "whisper",
        "first_run_done": True,
        "web_remote": web_remote,
        "gateway_port": 8765,
        "gateway_token": gateway_token,
    }
    if ollama_model:
        config["ollama_model"] = ollama_model
    if cloud_model:
        config["cloud_model"] = cloud_model
    if providers_dict:
        config["providers"] = providers_dict
    if email_address:
        config["email_address"] = email_address
    if morning_time:
        config["wake_up_time"] = morning_time
        config["wake_up_recurrence"] = morning_recurrence

    save_config(config)

    # Set up morning alarm if requested
    if morning_time:
        try:
            from alarms import AlarmManager
            am = AlarmManager()
            result = am.add_alarm(morning_time, alarm_type="morning",
                                  label="Morning wake up",
                                  recurrence=morning_recurrence)
            print(f"  {result}")
        except Exception as e:
            print(f"  (Morning alarm will be available after first start: {e})")

    # --- Introduction ---
    _clear_screen()
    print("=" * 60)
    print(f"  All set, {uname}! Meet {ainame} — your AI assistant.")
    print("=" * 60)
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
    if morning_time:
        print(f"    - Your morning alarm at {morning_time} includes motivation + briefing")
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
