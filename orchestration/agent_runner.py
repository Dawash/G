"""
Agent runner — runs desktop agent with mic-based interruption monitoring.

Extracted from brain.py::Brain._run_agent_mode().
Supports SwarmOrchestrator for complex multi-step tasks with automatic fallback
to legacy DesktopAgent.
"""

import logging
import re
import threading

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "stop", "cancel", "abort", "quit", "halt", "nevermind", "never mind",
})

# Detects complex multi-step goals like "plan+book", "research+write", "order+send"
_COMPLEX_TASK_RE = re.compile(
    r'\b(plan|book|order|create|research|build|organize|schedule|prepare)\b.+'
    r'\b(and|then|also|plus|after|with)\b.+'
    r'\b(send|post|upload|create|save|book|set|open|email|share)\b',
    re.I,
)

# UI-interactive tasks that benefit from multi-agent observation+verification
_UI_INTERACTIVE_RE = re.compile(
    r'\b(spotify|youtube)\b.*(play|search|find|watch|listen)|'
    r'(play|search|find|watch|listen).*(spotify|youtube)\b|'
    r'\b(order|book|buy|purchase)\b.*(online|pizza|food|ticket)',
    re.I,
)


def _is_complex_task(user_input):
    """Check if user_input is a complex or UI-interactive task for SwarmOrchestrator."""
    return bool(_COMPLEX_TASK_RE.search(user_input) or _UI_INTERACTIVE_RE.search(user_input))


class _CancellableSwarm:
    """Thin wrapper that gives SwarmOrchestrator a cancel() interface
    compatible with _monitor_for_interruption().
    """

    def __init__(self, swarm):
        self._swarm = swarm
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        # Set a flag on the blackboard so the state-machine loop can see it
        try:
            self._swarm._bb.set("cancelled", True)
        except Exception:
            pass
        logger.info("SwarmOrchestrator: cancel requested via agent runner")


def run_agent_mode(user_input, action_registry, reminder_mgr, speak_fn,
                   messages=None, skip_strategies=None, brain=None):
    """Run autonomous agent for multi-step screen tasks.

    Runs agent in background thread while monitoring mic for interruption.
    User can say 'stop', 'cancel', 'abort' to halt agent mid-task.
    Max 90s timeout. Agent uses llava vision + LLM reasoning.

    When *brain* is provided and the task matches complex multi-step patterns
    (e.g. "research X and write Y"), SwarmOrchestrator is tried first.
    On failure the legacy DesktopAgent is used as fallback.

    Args:
        user_input: Goal string for the agent.
        action_registry: Dict of intent -> handler function.
        reminder_mgr: ReminderManager instance.
        speak_fn: TTS function.
        messages: Optional message list to append result to.
        skip_strategies: Set of strategy names already tried by caller (avoid retrying).
        brain: Optional Brain instance (required for SwarmOrchestrator).

    Returns:
        str result message.
    """

    # --- Try SwarmOrchestrator for complex multi-step tasks ---
    if brain is not None and _is_complex_task(user_input):
        swarm_result = _try_swarm(user_input, brain, speak_fn, messages)
        if swarm_result is not None:
            return swarm_result
        # Swarm failed or was interrupted — fall through to legacy agent
        logger.info("Swarm failed or incomplete, falling back to legacy DesktopAgent")

    # --- Legacy DesktopAgent path ---
    return _run_legacy_agent(user_input, action_registry, reminder_mgr,
                             speak_fn, messages, skip_strategies)


def _try_swarm(user_input, brain, speak_fn, messages):
    """Attempt task via SwarmOrchestrator. Returns result str or None on failure."""
    try:
        from agents.orchestrator import SwarmOrchestrator
    except ImportError:
        logger.debug("SwarmOrchestrator not available, skipping")
        return None

    try:
        swarm = SwarmOrchestrator(brain, speak_fn=speak_fn)
        wrapper = _CancellableSwarm(swarm)

        result_holder = [None]
        error_holder = [None]

        def _run():
            try:
                result_holder[0] = swarm.execute(user_input)
            except Exception as e:
                error_holder[0] = e

        swarm_thread = threading.Thread(target=_run, daemon=True)
        swarm_thread.start()

        interrupted = _monitor_for_interruption(swarm_thread, wrapper)

        swarm_thread.join(timeout=5.0)

        if interrupted:
            return "OK, I've stopped the task."

        if error_holder[0]:
            logger.warning(f"Swarm failed: {error_holder[0]}")
            return None  # fall back to legacy

        result = result_holder[0]
        if result and "error" not in str(result).lower()[:30]:
            if messages is not None:
                messages.append({"role": "assistant", "content": str(result)})
            return result

        # Swarm returned error-ish result — fall back
        logger.info(f"Swarm returned partial/error result, falling back")
        return None

    except Exception as e:
        logger.warning(f"Swarm orchestrator error: {e}")
        return None


def _run_legacy_agent(user_input, action_registry, reminder_mgr, speak_fn,
                      messages, skip_strategies):
    """Run the legacy DesktopAgent with mic interruption monitoring."""
    from desktop_agent import DesktopAgent

    agent = DesktopAgent(
        action_registry=action_registry,
        reminder_mgr=reminder_mgr,
        speak_fn=speak_fn,
    )
    # Pass already-tried strategies so agent doesn't retry them
    if skip_strategies:
        agent._skip_strategies = set(skip_strategies)

    result_holder = [None]
    error_holder = [None]

    def _run_agent():
        try:
            result_holder[0] = agent.execute(user_input)
        except Exception as e:
            error_holder[0] = e

    agent_thread = threading.Thread(target=_run_agent, daemon=True)
    agent_thread.start()

    # Monitor for voice interruption while agent runs
    interrupted = _monitor_for_interruption(agent_thread, agent)

    # Wait for agent thread to finish (up to 5s after cancel)
    agent_thread.join(timeout=5.0)

    if interrupted:
        return "OK, I've stopped the task."

    if error_holder[0]:
        logger.error(f"Agent mode failed: {error_holder[0]}")
        return f"I had trouble completing that: {error_holder[0]}"

    result = result_holder[0]
    if result:
        if messages is not None:
            messages.append({"role": "assistant", "content": str(result)})
        return result
    return "I completed the task but couldn't confirm the result."


def _monitor_for_interruption(agent_thread, agent):
    """Monitor mic for stop commands while agent runs. Returns True if interrupted."""
    try:
        while agent_thread.is_alive():
            agent_thread.join(timeout=2.0)
            if not agent_thread.is_alive():
                break

            # Quick mic check for stop commands (non-blocking short listen)
            try:
                from speech import _get_whisper_model, _listen_vad_short, _is_noise
                wav = _listen_vad_short(max_speech_s=1.5, wait_timeout_s=0.5)
                if wav:
                    model = _get_whisper_model()
                    if model:
                        import os as _os
                        try:
                            segments, _ = model.transcribe(
                                wav, beam_size=1, language=None, vad_filter=False)
                            text = " ".join(
                                s.text.strip() for s in segments
                            ).strip().lower().rstrip(".,!?")
                            if text and not _is_noise(text):
                                words = set(text.split())
                                if words & _STOP_WORDS:
                                    logger.info(f"Agent interrupted by user: '{text}'")
                                    agent.cancel()
                                    return True
                        finally:
                            try:
                                _os.unlink(wav)
                            except OSError:
                                pass
            except Exception as e:
                logger.debug(f"Mic monitor error during agent: {e}")
    except Exception:
        pass
    return False
