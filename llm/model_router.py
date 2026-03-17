"""
Multi-Model Router — routes LLM requests to the optimal model for each task.

  "fast"      → Small local model for classification, extraction, yes/no
  "balanced"  → Main model for chat, tool calling, general tasks
  "powerful"  → Best available for complex reasoning, code review
  "vision"    → Vision model for screenshot analysis, image understanding
  "embedding" → Embedding model for vector similarity

Usage:
    from llm.model_router import model_router
    response = model_router.chat("What's 2+2?", task="fast")
    for token in model_router.stream("Hello", task="balanced"): ...
    category = model_router.classify("open Chrome", categories=["tool_use", "chat"])
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    tier: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 1024
    temperature: float = 0.7
    supports_tools: bool = True
    supports_vision: bool = False
    enabled: bool = True
    avg_latency_ms: float = 0.0
    error_count: int = 0
    last_error_time: float = 0.0


class ModelPool:
    """Pool of model configurations organized by tier with fallback support."""

    FALLBACK_CHAIN: Dict[str, List[str]] = {
        "fast":      ["balanced", "powerful"],
        "balanced":  ["powerful", "fast"],
        "powerful":  ["balanced"],
        "vision":    ["powerful", "balanced"],
        "embedding": [],
    }

    def __init__(self) -> None:
        self._models: Dict[str, List[ModelConfig]] = {
            t: [] for t in ("fast", "balanced", "powerful", "vision", "embedding")
        }
        self._lock = threading.Lock()

    def register(self, config: ModelConfig) -> None:
        with self._lock:
            tier_list = self._models.setdefault(config.tier, [])
            if not any(m.model == config.model and m.provider == config.provider
                       for m in tier_list):
                tier_list.append(config)
                logger.debug("ModelPool: registered %s/%s as '%s'",
                             config.provider, config.model, config.tier)

    def get_model(self, tier: str) -> Optional[ModelConfig]:
        with self._lock:
            for m in self._models.get(tier, []):
                if m.enabled and m.error_count < 5:
                    return m
            for fb in self.FALLBACK_CHAIN.get(tier, []):
                for m in self._models.get(fb, []):
                    if m.enabled and m.error_count < 5:
                        logger.debug("ModelPool: fallback %s → %s", tier, fb)
                        return m
        return None

    def mark_error(self, config: ModelConfig) -> None:
        config.error_count += 1
        config.last_error_time = time.time()
        if config.error_count >= 10:
            config.enabled = False
            logger.warning("ModelPool: disabled %s/%s after %d errors",
                           config.provider, config.model, config.error_count)

    def mark_success(self, config: ModelConfig, latency_ms: float) -> None:
        config.error_count = 0
        config.avg_latency_ms = (config.avg_latency_ms * 0.8 + latency_ms * 0.2
                                  if config.avg_latency_ms else latency_ms)

    def reset_errors(self) -> None:
        with self._lock:
            for tier_models in self._models.values():
                for m in tier_models:
                    if m.error_count > 0:
                        m.error_count = max(0, m.error_count - 1)
                    if not m.enabled and (time.time() - m.last_error_time) > 300:
                        m.enabled = True
                        logger.debug("ModelPool: re-enabled %s/%s", m.provider, m.model)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                tier: [
                    {"provider": m.provider, "model": m.model, "enabled": m.enabled,
                     "errors": m.error_count, "avg_latency_ms": round(m.avg_latency_ms, 1)}
                    for m in models
                ]
                for tier, models in self._models.items()
            }


# ── Task → tier mapping ───────────────────────────────────────────────────────

TASK_TIER_MAP: Dict[str, str] = {
    "classify":       "fast",
    "extract":        "fast",
    "yes_no":         "fast",
    "intent":         "fast",
    "sentiment":      "fast",
    "chat":           "balanced",
    "tool_call":      "balanced",
    "summarize":      "balanced",
    "translate":      "balanced",
    "quick_chat":     "balanced",
    "reason":         "powerful",
    "code_review":    "powerful",
    "code_write":     "powerful",
    "research":       "powerful",
    "analyze":        "powerful",
    "plan":           "powerful",
    "describe_image": "vision",
    "read_screenshot":"vision",
    "ocr":            "vision",
    "default":        "balanced",
}


class ModelRouter:
    """Routes LLM requests to the optimal model based on task type."""

    def __init__(self) -> None:
        self.pool = ModelPool()
        self._provider_cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._periodic_reset, daemon=True,
                         name="model-router-reset").start()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def setup_from_config(self, config: dict) -> None:
        """Populate the pool from config.json."""
        provider_name = config.get("provider", "ollama")
        api_key = config.get("api_key", "")
        ollama_model = config.get("ollama_model", "")
        ollama_url = config.get("ollama_url", "http://localhost:11434")
        cloud_model = config.get("cloud_model") or config.get("model", "")

        if provider_name == "ollama" and ollama_model:
            self.pool.register(ModelConfig(
                tier="balanced", provider="ollama", model=ollama_model,
                base_url=ollama_url, supports_tools=True,
            ))
            self._detect_ollama_models(ollama_url, ollama_model)

        elif provider_name == "openai":
            model = cloud_model or "gpt-4o-mini"
            self.pool.register(ModelConfig(
                tier="balanced", provider="openai", model=model,
                api_key=api_key, supports_tools=True,
                supports_vision="4o" in model or "gpt-4-turbo" in model,
            ))
            if model != "gpt-4o-mini":
                self.pool.register(ModelConfig(
                    tier="fast", provider="openai", model="gpt-4o-mini",
                    api_key=api_key, supports_tools=True,
                ))
            if "4o" in model:
                self.pool.register(ModelConfig(
                    tier="vision", provider="openai", model=model,
                    api_key=api_key, supports_vision=True,
                ))

        elif provider_name == "anthropic":
            model = cloud_model or "claude-sonnet-4-20250514"
            self.pool.register(ModelConfig(
                tier="balanced", provider="anthropic", model=model,
                api_key=api_key, supports_tools=True, supports_vision=True,
            ))
            if "opus" in model or "sonnet" in model:
                self.pool.register(ModelConfig(
                    tier="powerful", provider="anthropic", model=model,
                    api_key=api_key, supports_tools=True, supports_vision=True,
                ))
            self.pool.register(ModelConfig(
                tier="fast", provider="anthropic",
                model="claude-haiku-4-20250414",
                api_key=api_key, supports_tools=True,
            ))

        elif provider_name == "openrouter":
            model = cloud_model or "openai/gpt-4o-mini"
            self.pool.register(ModelConfig(
                tier="balanced", provider="openrouter", model=model,
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1/chat/completions",
                supports_tools=True,
            ))

        logger.info("ModelRouter: pool ready — %s", json.dumps(
            {k: [m["model"] for m in v] for k, v in self.pool.status().items() if v}
        ))

    def _detect_ollama_models(self, ollama_url: str, primary_model: str) -> None:
        try:
            import requests as _r
            resp = _r.get(f"{ollama_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                return
            models = [m["name"] for m in resp.json().get("models", [])]

            fast_kw    = ["phi3", "phi-3", "phi3.5", "qwen2.5:3b", "qwen2.5:7b",
                          "gemma2:2b", "llama3.2:3b", "tinyllama", "smollm"]
            vision_kw  = ["llava", "bakllava", "moondream", "llava-phi", "minicpm-v"]
            power_kw   = ["qwen2.5:72b", "llama3.1:70b", "deepseek-r1", "command-r-plus",
                          "mixtral:8x22b", "wizardlm2:8x22b"]
            embed_kw   = ["nomic-embed-text", "all-minilm", "mxbai-embed-large",
                          "snowflake-arctic-embed"]

            for name in models:
                if name == primary_model:
                    continue
                nl = name.lower()
                if any(k in nl for k in fast_kw):
                    self.pool.register(ModelConfig(
                        tier="fast", provider="ollama", model=name,
                        base_url=ollama_url, supports_tools=True))
                elif any(k in nl for k in vision_kw):
                    self.pool.register(ModelConfig(
                        tier="vision", provider="ollama", model=name,
                        base_url=ollama_url, supports_vision=True, supports_tools=False))
                elif any(k in nl for k in power_kw):
                    self.pool.register(ModelConfig(
                        tier="powerful", provider="ollama", model=name,
                        base_url=ollama_url, supports_tools=True))
                elif any(k in nl for k in embed_kw):
                    self.pool.register(ModelConfig(
                        tier="embedding", provider="ollama", model=name,
                        base_url=ollama_url, supports_tools=False))
        except Exception as e:
            logger.debug("ModelRouter: Ollama detection failed: %s", e)

    # ── Provider factory ──────────────────────────────────────────────────────

    def _get_provider(self, config: ModelConfig) -> Any:
        key = f"{config.provider}:{config.model}"
        with self._lock:
            if key in self._provider_cache:
                return self._provider_cache[key]

        try:
            from ai_providers import create_provider
            kw: Dict[str, Any] = {}
            if config.provider == "ollama":
                kw = {"ollama_model": config.model,
                      "ollama_url": config.base_url or "http://localhost:11434"}
            elif config.provider in ("openai", "anthropic", "openrouter"):
                kw = {"model": config.model}

            p = create_provider(config.provider, config.api_key, "", **kw)
            if p:
                with self._lock:
                    self._provider_cache[key] = p
            return p
        except Exception as e:
            logger.debug("ModelRouter: provider create failed %s: %s", key, e)
            return None

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_tier(self, task: str) -> str:
        return TASK_TIER_MAP.get(task, TASK_TIER_MAP["default"])

    def chat(self, prompt: str, task: str = "chat",
             system_prompt: str = "",
             messages: Optional[List[dict]] = None, **kwargs) -> Optional[str]:
        """Route a chat request to the optimal model tier."""
        tier = self.get_tier(task)
        config = self.pool.get_model(tier)
        if not config:
            logger.warning("ModelRouter: no model for tier '%s' (task: %s)", tier, task)
            return None

        provider = self._get_provider(config)
        if not provider:
            return None

        if messages is None:
            msgs: List[dict] = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": prompt})
        else:
            msgs = messages

        start = time.time()
        try:
            if system_prompt:
                provider.system_prompt = system_prompt
            provider.messages = [m for m in msgs if m.get("role") != "system"]
            result = provider._call_api()
            self.pool.mark_success(config, (time.time() - start) * 1000)
            return result
        except Exception as e:
            self.pool.mark_error(config)
            logger.debug("ModelRouter: %s/%s failed: %s", config.provider, config.model, e)
            # Fallback
            for fb_tier in ModelPool.FALLBACK_CHAIN.get(tier, []):
                fb = self.pool.get_model(fb_tier)
                if fb and fb is not config:
                    fp = self._get_provider(fb)
                    if fp:
                        try:
                            if system_prompt:
                                fp.system_prompt = system_prompt
                            fp.messages = [m for m in msgs if m.get("role") != "system"]
                            r = fp._call_api()
                            self.pool.mark_success(fb, (time.time() - start) * 1000)
                            return r
                        except Exception as e:
                            logger.debug("Fallback model %s failed: %s", fb.name, e)
                            self.pool.mark_error(fb)
        return None

    def stream(self, prompt: str, task: str = "chat",
               system_prompt: str = "",
               messages: Optional[List[dict]] = None,
               **kwargs) -> Generator[str, None, None]:
        """Stream a chat response routed to the optimal model tier."""
        tier = self.get_tier(task)
        config = self.pool.get_model(tier)
        if not config:
            return

        provider = self._get_provider(config)
        if not provider:
            return

        if messages is None:
            msgs: List[dict] = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": prompt})
        else:
            msgs = messages

        start = time.time()
        try:
            yield from provider.stream_response(msgs)
            self.pool.mark_success(config, (time.time() - start) * 1000)
        except Exception as e:
            self.pool.mark_error(config)
            logger.debug("ModelRouter: stream failed %s/%s: %s",
                         config.provider, config.model, e)

    def classify(self, text: str, categories: List[str],
                 system_prompt: str = "") -> str:
        """Classify text using the fast tier. Returns a category name."""
        cats = ", ".join(categories)
        prompt = (
            f"Classify the following text into exactly one of these categories: {cats}\n\n"
            f"Text: \"{text}\"\n\n"
            f"Reply with ONLY the category name, nothing else."
        )
        result = self.chat(
            prompt, task="classify",
            system_prompt=system_prompt or
            "You are a text classifier. Reply with only the category name.",
        )
        if result:
            clean = result.strip().lower()
            for cat in categories:
                if cat.lower() in clean or clean in cat.lower():
                    return cat
        return "unknown"

    def _periodic_reset(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.pool.reset_errors()
                # Publish pool status to bus (for HUD)
                try:
                    from core.event_bus import bus
                    bus.publish("system.model_pool", self.pool.status(),
                                source="model_router")
                except Exception as e:
                    logger.debug("model_router health-check bus publish failed: %s", e)
            except Exception as e:
                logger.debug("model_router health-check failed: %s", e)


# ── Singleton ─────────────────────────────────────────────────────────────────

model_router = ModelRouter()
