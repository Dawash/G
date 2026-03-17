"""Tests for llm/model_router.py — Multi-Model Routing (Option C)."""

from __future__ import annotations

import json
import os
import sys
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from llm.model_router import (
    ModelConfig, ModelPool, ModelRouter, TASK_TIER_MAP,
    model_router,
)


# =============================================================================
# ModelPool
# =============================================================================

class TestModelPool:
    def _pool(self):
        return ModelPool()

    def test_register_model(self):
        p = self._pool()
        p.register(ModelConfig(tier="balanced", provider="ollama", model="qwen2.5:7b"))
        assert len(p._models["balanced"]) == 1

    def test_register_duplicate_ignored(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="qwen2.5:7b")
        p.register(cfg)
        p.register(cfg)
        assert len(p._models["balanced"]) == 1

    def test_get_model_returns_enabled(self):
        p = self._pool()
        p.register(ModelConfig(tier="fast", provider="ollama", model="phi3"))
        m = p.get_model("fast")
        assert m is not None
        assert m.model == "phi3"

    def test_get_model_skips_disabled(self):
        p = self._pool()
        cfg = ModelConfig(tier="fast", provider="ollama", model="phi3", enabled=False)
        p.register(cfg)
        assert p.get_model("fast") is None

    def test_get_model_fallback_chain(self):
        p = self._pool()
        p.register(ModelConfig(tier="balanced", provider="ollama", model="qwen2.5:7b"))
        # fast tier empty → should fallback to balanced
        m = p.get_model("fast")
        assert m is not None
        assert m.model == "qwen2.5:7b"

    def test_get_model_returns_none_when_empty(self):
        p = self._pool()
        assert p.get_model("fast") is None

    def test_mark_error_increments(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m")
        p.register(cfg)
        p.mark_error(cfg)
        assert cfg.error_count == 1

    def test_mark_error_disables_after_10(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m")
        p.register(cfg)
        for _ in range(10):
            p.mark_error(cfg)
        assert not cfg.enabled

    def test_mark_success_resets_errors(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m", error_count=3)
        p.register(cfg)
        p.mark_success(cfg, 100.0)
        assert cfg.error_count == 0

    def test_mark_success_tracks_latency(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m")
        p.register(cfg)
        p.mark_success(cfg, 200.0)
        assert cfg.avg_latency_ms == 200.0
        p.mark_success(cfg, 100.0)
        assert cfg.avg_latency_ms == pytest.approx(180.0)  # 200*0.8 + 100*0.2

    def test_reset_errors_decrements(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m", error_count=3)
        p.register(cfg)
        p.reset_errors()
        assert cfg.error_count == 2

    def test_reset_errors_reenables_after_timeout(self):
        p = self._pool()
        cfg = ModelConfig(tier="balanced", provider="ollama", model="m",
                          enabled=False, last_error_time=time.time() - 400)
        p.register(cfg)
        p.reset_errors()
        assert cfg.enabled

    def test_status_returns_dict(self):
        p = self._pool()
        p.register(ModelConfig(tier="balanced", provider="ollama", model="qwen2.5:7b"))
        s = p.status()
        assert isinstance(s, dict)
        assert "balanced" in s
        assert s["balanced"][0]["model"] == "qwen2.5:7b"


# =============================================================================
# TASK_TIER_MAP
# =============================================================================

class TestTaskTierMap:
    def test_all_tasks_have_valid_tiers(self):
        valid = {"fast", "balanced", "powerful", "vision", "embedding"}
        for task, tier in TASK_TIER_MAP.items():
            assert tier in valid, f"task '{task}' has invalid tier '{tier}'"

    def test_no_duplicate_tasks(self):
        assert len(TASK_TIER_MAP) == len(set(TASK_TIER_MAP.keys()))


# =============================================================================
# ModelRouter.get_tier
# =============================================================================

class TestModelRouterGetTier:
    def test_get_tier_chat(self):
        r = ModelRouter()
        assert r.get_tier("chat") == "balanced"

    def test_get_tier_classify(self):
        r = ModelRouter()
        assert r.get_tier("classify") == "fast"

    def test_get_tier_code_write(self):
        r = ModelRouter()
        assert r.get_tier("code_write") == "powerful"

    def test_get_tier_describe_image(self):
        r = ModelRouter()
        assert r.get_tier("describe_image") == "vision"

    def test_get_tier_default(self):
        r = ModelRouter()
        assert r.get_tier("unknown_task_xyz") == "balanced"


# =============================================================================
# ModelRouter.setup_from_config
# =============================================================================

class TestSetupFromConfig:
    def test_setup_from_config_ollama(self):
        r = ModelRouter()
        r.setup_from_config({
            "provider": "ollama",
            "ollama_model": "qwen2.5:32b",
            "ollama_url": "http://localhost:11434",
        })
        m = r.pool.get_model("balanced")
        assert m is not None
        assert m.provider == "ollama"
        assert m.model == "qwen2.5:32b"

    def test_setup_from_config_openai(self):
        r = ModelRouter()
        r.setup_from_config({
            "provider": "openai",
            "api_key": "sk-test",
            "cloud_model": "gpt-4o",
        })
        m = r.pool.get_model("balanced")
        assert m is not None
        assert m.provider == "openai"

    def test_setup_from_config_anthropic(self):
        r = ModelRouter()
        r.setup_from_config({
            "provider": "anthropic",
            "api_key": "ant-test",
            "cloud_model": "claude-sonnet-4-20250514",
        })
        m = r.pool.get_model("balanced")
        assert m is not None
        assert m.provider == "anthropic"
        # Should also register fast (haiku) and powerful (sonnet)
        fast = r.pool.get_model("fast")
        assert fast is not None

    def test_setup_from_config_openrouter(self):
        r = ModelRouter()
        r.setup_from_config({
            "provider": "openrouter",
            "api_key": "or-test",
        })
        m = r.pool.get_model("balanced")
        assert m is not None
        assert m.provider == "openrouter"


# =============================================================================
# ModelRouter.classify
# =============================================================================

class TestClassify:
    def test_classify_returns_category(self):
        r = ModelRouter()
        mock_provider = MagicMock()
        mock_provider._call_api.return_value = "tool_use"
        r.pool.register(ModelConfig(tier="fast", provider="ollama", model="phi3"))
        with patch.object(r, "_get_provider", return_value=mock_provider):
            result = r.classify("open Chrome", categories=["tool_use", "chat", "agent"])
            assert result == "tool_use"

    def test_classify_returns_unknown_on_failure(self):
        r = ModelRouter()
        # No models registered — should return "unknown"
        result = r.classify("some text", categories=["a", "b"])
        assert result == "unknown"

    def test_classify_uses_fast_tier(self):
        r = ModelRouter()
        r.pool.register(ModelConfig(tier="fast", provider="ollama", model="phi3"))
        r.pool.register(ModelConfig(tier="balanced", provider="ollama", model="big"))

        selected = []
        orig_get = r._get_provider
        def _spy(cfg):
            selected.append(cfg.tier)
            mock = MagicMock()
            mock._call_api.return_value = "chat"
            return mock

        with patch.object(r, "_get_provider", side_effect=_spy):
            r.classify("hello", categories=["chat", "tool_use"])

        assert "fast" in selected


# =============================================================================
# ModelRouter.chat
# =============================================================================

class TestRouterChat:
    def test_chat_routes_to_correct_tier(self):
        r = ModelRouter()
        r.pool.register(ModelConfig(tier="balanced", provider="ollama", model="qwen"))
        mock_p = MagicMock()
        mock_p._call_api.return_value = "Hello!"

        with patch.object(r, "_get_provider", return_value=mock_p):
            result = r.chat("hi", task="chat")

        assert result == "Hello!"

    def test_chat_fallback_on_error(self):
        """When the primary model errors, the fallback tier is used."""
        r = ModelRouter()
        r.pool.register(ModelConfig(tier="fast", provider="ollama", model="phi3"))
        r.pool.register(ModelConfig(tier="balanced", provider="ollama", model="qwen"))

        call_log: list = []

        def _provider(cfg):
            m = MagicMock()
            call_log.append(cfg.model)
            if cfg.model == "phi3":
                m._call_api.side_effect = RuntimeError("model unavailable")
            else:
                m._call_api.return_value = "fallback response"
            return m

        with patch.object(r, "_get_provider", side_effect=_provider):
            result = r.chat("hi", task="fast")

        # The fallback should have been reached and returned its response
        assert result == "fallback response"
        # At least one provider was tried
        assert len(call_log) >= 1

    def test_chat_returns_none_when_no_model(self):
        r = ModelRouter()
        result = r.chat("hi", task="chat")
        assert result is None


# =============================================================================
# ModelRouter.stream
# =============================================================================

class TestRouterStream:
    def test_stream_yields_tokens(self):
        r = ModelRouter()
        r.pool.register(ModelConfig(tier="balanced", provider="ollama", model="qwen"))
        mock_p = MagicMock()
        mock_p.stream_response.return_value = iter(["Hello", " world"])

        with patch.object(r, "_get_provider", return_value=mock_p):
            tokens = list(r.stream("hi", task="chat"))

        assert tokens == ["Hello", " world"]

    def test_stream_empty_when_no_model(self):
        r = ModelRouter()
        tokens = list(r.stream("hi"))
        assert tokens == []


# =============================================================================
# Integration: real config
# =============================================================================

class TestIntegration:
    def test_router_with_real_config(self):
        cfg_path = os.path.join(ROOT, "config.json")
        if not os.path.exists(cfg_path):
            pytest.skip("config.json not found")

        with open(cfg_path) as f:
            cfg = json.load(f)

        r = ModelRouter()
        r.setup_from_config(cfg)
        status = r.pool.status()
        # At least one tier must be populated
        total = sum(len(v) for v in status.values())
        assert total > 0

    def test_brain_has_route_chat(self):
        from brain import Brain
        assert hasattr(Brain, "route_chat")

    def test_brain_has_route_stream(self):
        from brain import Brain
        assert hasattr(Brain, "route_stream")

    def test_assistant_loop_wires_router(self):
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path) as f:
            src = f.read()
        assert "model_router" in src
        assert "setup_from_config" in src
