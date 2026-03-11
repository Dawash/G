import os
import json
import random
import logging
import time
import requests

from config import RESPONSE_FILE
from core.state import ProviderState as _ProviderState

MAX_CONTEXT_MESSAGES = 20  # Keep last N messages to avoid unbounded growth

# --- Provider state — delegated to core.state.ProviderState ---
_provider_state = _ProviderState()  # Shared provider state (will be injected by container later)


def is_rate_limited(provider_name=None):
    """Check if a specific provider (or ALL providers) is in rate-limit cooldown.

    Args:
        provider_name: Check a specific provider. If None, returns True only if
                       ALL known providers are rate limited (backward compat).
    """
    if provider_name is not None:
        return _provider_state.is_rate_limited(provider_name)
    # No provider specified — return True if ALL known providers are limited
    with _provider_state._lock:
        if not _provider_state.rate_limits:
            return False
        now = time.time()
        # Copy values to avoid RuntimeError if dict changes during iteration
        entries = list(_provider_state.rate_limits.values())
    return all(now < entry.get("until", 0.0) for entry in entries)


def _record_rate_limit(provider_name="unknown"):
    """Record a 429 for a specific provider and set exponential backoff."""
    with _provider_state._lock:
        if provider_name not in _provider_state.rate_limits:
            _provider_state.rate_limits[provider_name] = {"until": 0.0, "consecutive": 0}
        entry = _provider_state.rate_limits[provider_name]
        entry["consecutive"] += 1
        # Exponential backoff: 10s, 20s, 40s, 60s max
        wait = min(10 * (2 ** (entry["consecutive"] - 1)), 60)
        entry["until"] = time.time() + wait
        consecutive = entry["consecutive"]
    logging.warning(f"Rate limited (429) for {provider_name}. Backing off for {wait}s. "
                    f"({consecutive} consecutive)")
    return wait


def _clear_rate_limit(provider_name="unknown"):
    """Clear rate limit tracking for a specific provider after a successful call."""
    _provider_state.clear_rate_limit(provider_name)


# --- Ollama health monitoring ---


def check_ollama_health(force=False, ollama_url=None):
    """Periodic Ollama health check. Returns True if available."""
    if not force and not _provider_state.should_check_ollama():
        with _provider_state._lock:
            return _provider_state.ollama_available
    available = OllamaProvider.is_available(ollama_url=ollama_url)
    _provider_state.set_ollama_status(available)
    return available


class ChatProvider:
    """Base class for AI chat providers."""

    def __init__(self, api_key, system_prompt):
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.messages = []  # Sliding window of conversation
        self.provider_name = "unknown"  # Overridden by subclasses

    def chat(self, user_input):
        # Skip API call if this provider is rate-limited
        if is_rate_limited(self.provider_name):
            entry = _provider_state.rate_limits.get(self.provider_name, {})
            remaining = int(entry.get("until", 0) - time.time())
            logging.info(f"Skipping API call — {self.provider_name} rate limited for {remaining}s more")
            return self._offline_fallback(user_input, rate_limited=True)

        self.messages.append({"role": "user", "content": user_input})
        self._trim_context()

        try:
            reply = self._call_api()
            self.messages.append({"role": "assistant", "content": reply})
            _clear_rate_limit(self.provider_name)
            store_conversation(user_input, reply)
            return reply
        except requests.HTTPError as e:
            logging.error(f"API call failed ({self.provider_name}): {e}")
            self.messages.pop()  # Permanent error — remove message
            if e.response is not None and e.response.status_code == 429:
                wait = _record_rate_limit(self.provider_name)
                return f"I'm being rate limited by the API. I'll retry in {wait} seconds. Try again shortly."
            return self._offline_fallback(user_input)
        except (requests.ConnectionError, requests.Timeout) as e:
            # Transient error — keep the user message so it can be retried
            logging.error(f"API call failed ({self.provider_name}, transient): {e}")
            # Do NOT pop — message stays in history for automatic retry on next call
            return self._offline_fallback(user_input)
        except Exception as e:
            logging.error(f"API call failed ({self.provider_name}): {e}")
            self.messages.pop()  # Unknown error — remove message to avoid corruption
            return self._offline_fallback(user_input)

    def _call_api(self):
        raise NotImplementedError

    def _trim_context(self):
        if len(self.messages) > MAX_CONTEXT_MESSAGES:
            self.messages = self.messages[-MAX_CONTEXT_MESSAGES:]

    def _offline_fallback(self, user_input, rate_limited=False):
        cached = get_offline_response(user_input)
        if cached:
            return f"From our past chats: {cached}"
        if rate_limited:
            return "I'm rate limited by the API right now. Try again in a moment, or say a simpler command."
        return "I can't reach the internet right now. Check your connection, or try a local command."


class OllamaProvider(ChatProvider):
    """Ollama — local LLM, no API key, no rate limits."""

    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, api_key, system_prompt, model="qwen2.5:7b", ollama_url=None):
        super().__init__(api_key, system_prompt)
        self.provider_name = "ollama"
        self.model = model
        self.ollama_url = (ollama_url or self.DEFAULT_BASE_URL).rstrip("/")

    def _call_api(self):
        response = requests.post(
            f"{self.ollama_url}/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *self.messages,
                ],
            },
            timeout=60,  # First call can be slow (model loading), complex tasks need more time
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def is_available(ollama_url=None):
        """Check if Ollama server is reachable."""
        base = (ollama_url or OllamaProvider.DEFAULT_BASE_URL).rstrip("/")
        try:
            resp = requests.get(base, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False


class OpenRouterProvider(ChatProvider):
    """OpenRouter API — supports many models."""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.0-flash-001"

    def __init__(self, api_key, system_prompt, model=None):
        super().__init__(api_key, system_prompt)
        self.provider_name = "openrouter"
        self.model = model or self.DEFAULT_MODEL

    def _call_api(self):
        response = requests.post(
            self.BASE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *self.messages,
                ],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


class OpenAIProvider(ChatProvider):
    """OpenAI API (GPT-4o-mini, GPT-4o, etc.)."""

    BASE_URL = "https://api.openai.com/v1/chat/completions"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key, system_prompt, model=None):
        super().__init__(api_key, system_prompt)
        self.provider_name = "openai"
        self.model = model or self.DEFAULT_MODEL

    def _call_api(self):
        response = requests.post(
            self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *self.messages,
                ],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


class AnthropicProvider(ChatProvider):
    """Anthropic API (Claude models)."""

    BASE_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    def __init__(self, api_key, system_prompt, model=None):
        super().__init__(api_key, system_prompt)
        self.provider_name = "anthropic"
        self.model = model or self.DEFAULT_MODEL

    def _call_api(self):
        response = requests.post(
            self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": self.system_prompt,
                "messages": self.messages,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"]


def create_provider(provider_name, api_key, system_prompt, ollama_model=None, ollama_url=None, model=None):
    """Factory function — returns the right provider for the user's choice."""
    if provider_name == "ollama":
        return OllamaProvider(api_key, system_prompt, model=ollama_model or "qwen2.5:7b",
                              ollama_url=ollama_url)

    providers = {
        "openrouter": OpenRouterProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }
    cls = providers.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}. Use: ollama, {list(providers.keys())}")
    return cls(api_key, system_prompt, model=model)


# --- Conversation storage (shared across providers) ---

def store_conversation(user_input, ai_response):
    """Append a conversation turn to the response file."""
    entry = {"user": user_input, "ai": ai_response}

    data = []
    if os.path.exists(RESPONSE_FILE):
        try:
            with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = []

    data.append(entry)

    with open(RESPONSE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_offline_response(user_input):
    """Find a cached response matching the user's input."""
    if not os.path.exists(RESPONSE_FILE):
        return None

    try:
        with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
            conversations = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

    matches = [
        conv["ai"]
        for conv in conversations
        if conv.get("user") and user_input.lower() in conv["user"].lower()
    ]
    return random.choice(matches) if matches else None
