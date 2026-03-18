import os
import json
import random
import logging
import time
import requests

from config import RESPONSE_FILE
from core.state import ProviderState as _ProviderState
from core.timeouts import Timeouts

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
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(OllamaProvider.is_available, ollama_url)
            available = _fut.result(timeout=Timeouts.OLLAMA_HEALTH)
    except (concurrent.futures.TimeoutError, Exception):
        available = False
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
            try:
                from core.observability import metrics as _obs
                _obs.record_failure("llm.chat", error=str(e)[:200])
            except Exception:
                pass
            if e.response is not None and e.response.status_code == 429:
                wait = _record_rate_limit(self.provider_name)
                return f"I'm being rate limited by the API. I'll retry in {wait} seconds. Try again shortly."
            return self._offline_fallback(user_input)
        except (requests.ConnectionError, requests.Timeout) as e:
            # Transient error — pop message to prevent stale duplication on next call
            logging.error(f"API call failed ({self.provider_name}, transient): {e}")
            self.messages.pop()
            try:
                from core.observability import metrics as _obs
                _obs.record_failure("llm.chat", error=str(e)[:200])
            except Exception:
                pass
            return self._offline_fallback(user_input)
        except Exception as e:
            logging.error(f"API call failed ({self.provider_name}): {e}")
            self.messages.pop()  # Unknown error — remove message to avoid corruption
            try:
                from core.observability import metrics as _obs
                _obs.record_failure("llm.chat", error=str(e)[:200])
            except Exception:
                pass
            return self._offline_fallback(user_input)

    def _call_api(self):
        raise NotImplementedError

    def stream_response(self, messages_with_system):
        """Fallback: non-streaming providers yield the full response as one token."""
        try:
            # Build a temporary message state to call _call_api
            saved = self.messages[:]
            self.messages = [m for m in messages_with_system if m["role"] != "system"]
            result = self._call_api()
            self.messages = saved
            if result:
                yield result
        except Exception:
            return

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

    def _get_timeout(self, warm=False):
        """Get timeout based on model size. Larger models need more time."""
        model_lower = self.model.lower()
        if "72b" in model_lower or "70b" in model_lower:
            return Timeouts.BRAIN_THINK_72B if not warm else Timeouts.BRAIN_THINK_72B * 2
        if "32b" in model_lower or "34b" in model_lower:
            return Timeouts.BRAIN_THINK_32B if not warm else Timeouts.BRAIN_WARM_32B + 100
        if "14b" in model_lower or "13b" in model_lower:
            return Timeouts.BRAIN_THINK_14B if not warm else Timeouts.BRAIN_WARM_14B + 30
        return Timeouts.LLM_CHAT if not warm else Timeouts.LLM_STREAM  # 7b and smaller

    def _call_api(self):
        # Try native Ollama endpoint first (works on all versions)
        # Falls back to OpenAI-compatible /v1/ if native fails
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self.messages,
        ]
        timeout = self._get_timeout()
        _api_t0 = time.time()
        try:
            response = requests.post(
                f"{self.ollama_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            result = data["message"]["content"]
            try:
                from core.observability import metrics as _obs
                _obs.record_success("llm.chat", duration_ms=(time.time() - _api_t0) * 1000)
            except Exception:
                pass
            return result
        except requests.exceptions.HTTPError:
            # Fallback to OpenAI-compatible endpoint (newer Ollama)
            response = requests.post(
                f"{self.ollama_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            result = data["choices"][0]["message"]["content"]
            try:
                from core.observability import metrics as _obs
                _obs.record_success("llm.chat", duration_ms=(time.time() - _api_t0) * 1000)
            except Exception:
                pass
            return result

    def stream_response(self, messages_with_system):
        """Stream tokens from Ollama one at a time.

        Uses Ollama's native /api/chat with stream=True which returns NDJSON.
        Each line is: {"message": {"content": "<token>"}, "done": false}

        Args:
            messages_with_system: List of {role, content} dicts including system message.

        Yields:
            str: Individual text tokens as they arrive from the model.
        """
        timeout = self._get_timeout()
        try:
            response = requests.post(
                f"{self.ollama_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages_with_system,
                    "stream": True,
                },
                stream=True,
                timeout=timeout,
            )
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if data.get("done"):
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            logging.debug(f"Ollama stream_response failed: {e}")
            return

    @staticmethod
    def is_available(ollama_url=None):
        """Check if Ollama server is reachable."""
        base = (ollama_url or OllamaProvider.DEFAULT_BASE_URL).rstrip("/")
        try:
            resp = requests.get(base, timeout=Timeouts.OLLAMA_HEALTH)
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
            timeout=Timeouts.LLM_STREAM,
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
            timeout=Timeouts.LLM_STREAM,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def stream_response(self, messages_with_system):
        """Stream tokens from OpenAI via SSE (Server-Sent Events).

        Yields:
            str: Individual text tokens as they arrive.
        """
        try:
            response = requests.post(
                self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages_with_system,
                    "stream": True,
                },
                stream=True,
                timeout=Timeouts.LLM_STREAM,
            )
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except Exception as e:
            logging.debug(f"OpenAI stream_response failed: {e}")
            return


class AnthropicProvider(ChatProvider):
    """Anthropic API (Claude models)."""

    BASE_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    # Models that support extended thinking (exclude haiku)
    _THINKING_CAPABLE = ("claude-opus-4", "claude-sonnet-4", "claude-3-7")

    def __init__(self, api_key, system_prompt, model=None):
        super().__init__(api_key, system_prompt)
        self.provider_name = "anthropic"
        self.model = model or self.DEFAULT_MODEL

    def _supports_thinking(self) -> bool:
        """Check if the current model supports extended thinking."""
        m = self.model.lower()
        return any(x in m for x in self._THINKING_CAPABLE) and "haiku" not in m

    def _call_api(self):
        thinking_enabled = self._supports_thinking()
        max_tokens = 8000 if thinking_enabled else 2048

        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": self.messages,
        }

        # System prompt as cached array for prompt caching
        payload["system"] = [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Extended thinking — requires temperature=1, omitted so it defaults to 1
        if thinking_enabled:
            payload["thinking"] = {"type": "enabled", "budget_tokens": 3000}

        response = requests.post(
            self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2024-06-01",
                "Content-Type": "application/json",
                "anthropic-beta": "prompt-caching-2024-07-16",
            },
            json=payload,
            timeout=Timeouts.LLM_STREAM,
        )
        response.raise_for_status()
        data = response.json()

        # Extract text blocks only (skip thinking blocks from spoken output)
        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "thinking":
                logging.debug(f"[Claude thinking] {block.get('thinking', '')[:400]}")
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        return " ".join(text_parts) if text_parts else ""

    def stream_response(self, messages_with_system):
        """Stream tokens from Anthropic via SSE.

        Yields:
            str: Individual text tokens as they arrive.
        """
        try:
            # Pull system out of messages_with_system if present
            system_text = self.system_prompt
            user_messages = [m for m in messages_with_system if m["role"] != "system"]

            response = requests.post(
                self.BASE_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2024-06-01",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1024,
                    "system": system_text,
                    "messages": user_messages,
                    "stream": True,
                },
                stream=True,
                timeout=Timeouts.LLM_STREAM,
            )
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        data = json.loads(data_str)
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    yield text
                        elif data.get("type") == "message_stop":
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            logging.debug(f"Anthropic stream_response failed: {e}")
            return


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
