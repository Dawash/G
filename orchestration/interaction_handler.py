"""
Layered interaction handler — processes user input through ordered layers.

Each layer returns Optional[InteractionResult]:
  - None  → layer didn't handle it, try next layer
  - InteractionResult → handled, stop processing

Layer order:
  1. Exit detection        (_check_exit)
  2. Connection toggle     (_check_connection)
  3. Provider switch       (_check_provider_switch)
  4. Meta-commands         (_check_meta_commands)
  5. Plugins               (_check_plugins)
  6. Fast-path             (_check_fast_path)
  7. Brain (LLM)           (_run_brain)
  8. Keyword fallback      (_run_fallback)

This module is imported by assistant_loop.py and used for simple
layer classification and routing.  The complex streaming/timeout
logic for brain.think() still lives in assistant_loop.py.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Result dataclass
# ===================================================================

@dataclass
class InteractionResult:
    """Outcome of processing user input through the interaction layers."""
    response: Optional[str] = None
    should_exit: bool = False
    should_sleep: bool = False
    handled_by: str = ""
    duration_ms: int = 0
    was_streamed: bool = False
    tools_used: List[str] = field(default_factory=list)
    error: str = ""


# ===================================================================
# Handler
# ===================================================================

class InteractionHandler:
    """Routes user input through ordered layers, returning the first match.

    Args:
        brain: Brain instance (or None if unavailable).
        config: Config dict (keys: ainame, username, provider, etc.).
        services: Dict of shared services (memory, reminder_mgr, action_map, provider).
    """

    def __init__(self, brain, config: dict, services: dict):
        self.brain = brain
        self.config = config
        self.services = services
        self.last_response: Optional[str] = None
        self._undo_fns: List[Callable] = []
        self._plugin_loader = None  # Set externally if plugins are loaded

    # ----- Public API -----

    def process(self, user_input: str) -> InteractionResult:
        """Run all layers in order; return the first result that handles the input.

        Returns:
            InteractionResult with handled_by set to the layer name,
            or a fallback result if nothing matched.
        """
        t0 = time.time()

        layers = [
            self._check_exit,
            self._check_connection,
            self._check_provider_switch,
            self._check_meta_commands,
            self._check_plugins,
            self._check_fast_path,
            self._run_brain,
            self._run_fallback,
        ]

        for layer_fn in layers:
            try:
                result = layer_fn(user_input)
                if result is not None:
                    result.duration_ms = int((time.time() - t0) * 1000)
                    # Track last response for repeat/shorter
                    if result.response:
                        self.last_response = result.response
                    return result
            except Exception as e:
                logger.error(f"Layer {layer_fn.__name__} failed: {e}", exc_info=True)

        # Nothing handled it
        return InteractionResult(
            response=None,
            handled_by="unhandled",
            duration_ms=int((time.time() - t0) * 1000),
        )

    def get_layer_for_input(self, user_input: str) -> str:
        """Classify which layer would handle this input (without executing).

        Returns:
            str: Layer name ("exit", "connection", "provider_switch",
                 "meta_*", "plugin", "fast_path", "brain", "fallback", "unhandled").
        """
        # Exit
        try:
            from orchestration.command_router import is_exit_command
            if is_exit_command(user_input):
                return "exit"
        except ImportError:
            pass
        lower = user_input.lower().strip()
        if lower in ("goodbye", "bye", "exit", "quit", "go to sleep"):
            return "exit"

        # Connection
        try:
            from orchestration.command_router import is_connection_command
            conn = is_connection_command(user_input)
            if conn:
                return "connection"
        except ImportError:
            pass
        if lower in ("disconnect", "go offline"):
            return "connection"

        # Provider switch
        try:
            from orchestration.command_router import check_provider_switch
            if check_provider_switch(user_input):
                return "provider_switch"
        except ImportError:
            pass

        # Meta-commands
        try:
            from orchestration.command_router import detect_meta_command
            meta = detect_meta_command(user_input)
            if meta:
                if isinstance(meta, tuple):
                    return f"meta_{meta[0]}"
                return f"meta_{meta}"
        except ImportError:
            pass
        if lower in ("skip", "stop"):
            return "meta_skip"
        if lower in ("repeat", "say that again"):
            return "meta_repeat"

        # Plugin
        if self._plugin_loader:
            try:
                if self._plugin_loader.can_handle(user_input):
                    return "plugin"
            except Exception:
                pass

        # Fast-path
        try:
            from orchestration.fast_path import match_fast_path
            decision = match_fast_path(user_input)
            if decision and decision.is_deterministic:
                return "fast_path"
        except Exception:
            pass

        # Brain available?
        if self.brain:
            return "brain"

        return "fallback"

    def register_undo(self, undo_fn: Callable):
        """Register a callable for undo support."""
        self._undo_fns.append(undo_fn)

    def set_plugin_loader(self, loader):
        """Attach the plugin loader for Layer 5 (plugin intents)."""
        self._plugin_loader = loader

    # ----- Layer implementations -----

    def _check_exit(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 1a: Exit / goodbye detection."""
        try:
            from orchestration.command_router import is_exit_command
            if is_exit_command(user_input):
                return InteractionResult(should_exit=True, handled_by="exit")
        except ImportError:
            pass
        # Fallback: basic keyword check
        lower = user_input.lower().strip()
        if lower in ("goodbye", "bye", "exit", "quit", "go to sleep"):
            return InteractionResult(should_exit=True, handled_by="exit")
        return None

    def _check_connection(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 1b: Disconnect / reconnect toggle."""
        try:
            from orchestration.command_router import is_connection_command
            conn = is_connection_command(user_input)
            if conn == "disconnect":
                return InteractionResult(
                    response="Going offline. Local commands still work.",
                    handled_by="connection_disconnect",
                )
            elif conn == "connect":
                return InteractionResult(
                    response="Back online.",
                    handled_by="connection_connect",
                )
        except ImportError:
            pass
        # Fallback
        lower = user_input.lower().strip()
        if lower in ("disconnect", "go offline"):
            return InteractionResult(
                response="Going offline. Local commands still work.",
                handled_by="connection_disconnect",
            )
        if lower == "connect me":
            return InteractionResult(
                response="Back online.",
                handled_by="connection_connect",
            )
        return None

    def _check_provider_switch(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 1c: Provider switch (e.g. 'switch to openai')."""
        try:
            from orchestration.command_router import check_provider_switch
            match = check_provider_switch(user_input)
            if match:
                return InteractionResult(
                    response=None,  # Caller handles the actual switch
                    handled_by=f"provider_switch_{match}",
                )
        except ImportError:
            pass
        return None

    def _check_meta_commands(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 2: Meta-commands — skip, shorter, repeat, undo, etc."""
        try:
            from orchestration.command_router import detect_meta_command
            meta = detect_meta_command(user_input)
            if meta:
                if isinstance(meta, tuple) and meta[0] == "correction":
                    return InteractionResult(
                        response=meta[1],  # corrected text
                        handled_by="meta_correction",
                    )
                return InteractionResult(
                    response=None,
                    handled_by=f"meta_{meta}",
                )
        except ImportError:
            pass
        # Fallback check for common meta words
        lower = user_input.lower().strip()
        if lower in ("skip", "stop"):
            return InteractionResult(response=None, handled_by="meta_skip")
        if lower in ("repeat", "say that again"):
            return InteractionResult(
                response=self.last_response or "Nothing to repeat.",
                handled_by="meta_repeat",
            )
        return None

    def _check_plugins(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 3: Plugin intents (Mycroft-style skill system)."""
        if not self._plugin_loader:
            return None
        try:
            result = self._plugin_loader.try_handle(user_input)
            if result:
                return InteractionResult(
                    response=str(result),
                    handled_by="plugin",
                )
        except Exception as e:
            logger.debug(f"Plugin check failed: {e}")
        return None

    def _check_fast_path(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 4: Deterministic fast-path routing (no LLM)."""
        try:
            from orchestration.fast_path import try_fast_path
            fp_result = try_fast_path(user_input, self.services.get("action_map", {}))
            if fp_result and fp_result.response:
                return InteractionResult(
                    response=str(fp_result.response),
                    handled_by="fast_path",
                )
        except Exception as e:
            logger.debug(f"Fast-path check failed: {e}")
        return None

    def _run_brain(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 5: Brain (LLM tool-calling).

        NOTE: This is a simplified version.  The full streaming/timeout
        logic with acknowledgment timer still lives in assistant_loop.py.
        """
        if not self.brain:
            return None
        try:
            response = self.brain.think(user_input)
            if response:
                return InteractionResult(response=str(response), handled_by="brain")
        except Exception as e:
            logger.error(f"Brain failed: {e}")
        return None

    def _run_fallback(self, user_input: str) -> Optional[InteractionResult]:
        """Layer 6: Keyword intent detection fallback (offline-capable)."""
        try:
            from intent import detect_intent, INTENT_CHAT
            provider_name = self.config.get("provider", "ollama")
            api_key = self.config.get("api_key", "")
            action_map = self.services.get("action_map", {})

            actions = detect_intent(
                user_input, provider_name=provider_name,
                api_key=api_key, use_ai=False,
            )

            for intent, data in actions:
                if intent == INTENT_CHAT:
                    # Chat needs LLM — can't handle in pure fallback
                    continue
                handler = action_map.get(intent)
                if handler:
                    result = handler(data) if data else handler()
                    if result:
                        return InteractionResult(
                            response=str(result),
                            handled_by=f"fallback_{intent}",
                        )
        except Exception as e:
            logger.debug(f"Fallback routing failed: {e}")
        return None
