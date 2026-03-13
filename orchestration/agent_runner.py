"""
Agent runner — runs desktop agent with mic-based interruption monitoring.

Extracted from brain.py::Brain._run_agent_mode().
"""

import logging
import threading

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "stop", "cancel", "abort", "quit", "halt", "nevermind", "never mind",
})


def run_agent_mode(user_input, action_registry, reminder_mgr, speak_fn,
                   messages=None, skip_strategies=None):
    """Run autonomous agent for multi-step screen tasks.

    Runs agent in background thread while monitoring mic for interruption.
    User can say 'stop', 'cancel', 'abort' to halt agent mid-task.
    Max 90s timeout. Agent uses llava vision + LLM reasoning.

    Args:
        user_input: Goal string for the agent.
        action_registry: Dict of intent -> handler function.
        reminder_mgr: ReminderManager instance.
        speak_fn: TTS function.
        messages: Optional message list to append result to.
        skip_strategies: Set of strategy names already tried by caller (avoid retrying).

    Returns:
        str result message.
    """
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
