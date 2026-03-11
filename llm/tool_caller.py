"""
Tool calling engine — executes LLM tool-calling loops.

Extracted from brain.py. Two modes:
  1. Native tool calling (OpenAI format) — LLM returns tool_calls in response
  2. Prompt-based tool calling — LLM outputs JSON actions in text

Both modes handle hallucination detection, multi-round execution, and
fallback between modes.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3


def think_native(brain):
    """Process with native function/tool calling.

    Runs up to MAX_TOOL_ROUNDS rounds of:
      1. Call LLM with tool definitions
      2. Extract tool calls (native or from JSON in text)
      3. Execute tools, feed results back
      4. Repeat until LLM returns plain text

    Args:
        brain: Brain instance (provides messages, LLM calls, tool execution, etc.)

    Returns:
        str response or None.
    """
    from brain import (
        execute_tool, log_action, _resolve_tool_name, _extract_tool_from_json,
        _looks_like_json_garbage, _build_prompt_system, _CORE_TOOL_NAMES,
    )

    _seen_tools = []  # Track tool calls to detect hallucination loops
    for round_num in range(MAX_TOOL_ROUNDS):
        # Check cancellation between rounds
        if getattr(brain, '_cancelled', False):
            logger.info("Brain cancelled between tool rounds")
            return None

        response = brain._call_llm_native()

        if response is None:
            # Transient failure — fall back to prompt mode on ANY round.
            if brain.provider_name == "ollama":
                logger.info(f"Native tool call returned None (round {round_num}) — prompt fallback")
                if round_num == 0:
                    brain._pop_user_message()
                saved_prompt = brain.system_prompt
                brain.system_prompt = _build_prompt_system(brain.username, brain.ainame)
                result = think_prompt_based(brain)
                brain.system_prompt = saved_prompt
                return result
            if round_num == 0:
                brain._pop_user_message()
            return None

        message = response.get("message", {})
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            content = message.get("content", "")

            # Check if the LLM dumped tool JSON into text content
            extracted = _extract_tool_from_json(content) if content else []
            if extracted:
                result = _handle_extracted_tools(brain, extracted, content, round_num)
                if result is not None:
                    return result

            # Check if content looks like unparseable JSON (LLM garbage)
            if content and _looks_like_json_garbage(content):
                extracted = _extract_tool_from_json(content)
                if extracted:
                    result = _handle_garbage_json(brain, extracted, content)
                    if result is not None:
                        return result

                # No tools extracted — try to salvage as plain text first.
                # Strip JSON artifacts and check if readable text remains.
                cleaned = re.sub(r'```(?:json)?.*?```', '', content, flags=re.DOTALL)
                cleaned = re.sub(r'\{[^{}]*\}', '', cleaned)
                cleaned = cleaned.strip()
                if len(cleaned) > 10:
                    logger.info("Garbage JSON but readable text found — returning cleaned")
                    cleaned = brain._sanitize_response(cleaned)
                    brain.messages.append({"role": "assistant", "content": cleaned})
                    return cleaned

                # Truly unparseable — fall back to prompt mode
                logger.info("Native mode returned unparseable JSON, retrying prompt-based")
                saved_prompt = brain.system_prompt
                brain.system_prompt = _build_prompt_system(brain.username, brain.ainame)
                result = think_prompt_based(brain)
                brain.system_prompt = saved_prompt
                return result

            # Check if content is a refusal (LLM giving instructions instead of using tools)
            if content and brain._is_llm_refusal(content) and round_num == 0:
                logger.info("Native mode LLM refused to use tools, retrying")
                tool_hint = brain._suggest_tool_for_retry(
                    brain.messages[-1].get("content", "") if brain.messages else "")
                brain.messages.append({"role": "assistant", "content": "Let me use my tools."})
                brain.messages.append({
                    "role": "user",
                    "content": f"Do NOT give instructions. Use a tool to do this directly on the computer. {tool_hint}"
                })
                continue  # Retry the LLM call with the hint

            # Response validation: detect when LLM gives instructions instead of
            # using tools for system queries. Directly execute the right tool.
            if content and round_num == 0:
                direct_result = _validate_and_redirect(brain, content)
                if direct_result is not None:
                    return direct_result

            # Plain text response — sanitize and return
            content = brain._sanitize_response(content)
            brain.messages.append({"role": "assistant", "content": content})
            return content

        # Execute tool calls — PARALLEL for speed when multiple tools
        brain.messages.append(message)

        if len(tool_calls) == 1:
            result = _execute_single_tool(
                brain, tool_calls[0], _seen_tools, round_num)
            if result == "__CONTINUE__":
                continue
            if result is not None:
                return result
        else:
            _execute_parallel_tools(brain, tool_calls, round_num)
            # After parallel execution, check if Brain was cancelled
            if getattr(brain, '_cancelled', False):
                # Return a reasonable summary instead of calling LLM again
                tool_names = [tc["function"]["name"] for tc in tool_calls]
                return f"Done! Completed: {', '.join(tool_names)}."

    logger.warning("Brain hit max tool rounds")
    return "Done! I've completed the actions."


def think_prompt_based(brain):
    """Process with prompt-based tool calling (JSON in LLM output).

    Args:
        brain: Brain instance.

    Returns:
        str response or None.
    """
    from brain import execute_tool, _resolve_tool_name, _parse_prompt_actions

    response = brain._call_llm_simple()
    if response is None:
        brain._pop_user_message()
        return None

    content = response.get("content", "")
    if not content:
        brain._pop_user_message()
        return None

    # Detect LLM refusal and retry with stronger instruction
    if brain._is_llm_refusal(content):
        content = _handle_refusal_retry(brain, content)

    # Parse actions from the LLM output
    actions, spoken = _parse_prompt_actions(content)

    # Execute any actions the LLM requested
    results = []
    for action in actions:
        tool_name = action.get("tool", "")
        tool_args = action.get("args", {})
        if not tool_name:
            continue

        resolved = _resolve_tool_name(tool_name)
        if not resolved:
            logger.warning(f"Brain prompt-based hallucinated tool: {tool_name}")
            continue
        tool_name = resolved

        logger.info(f"Brain prompt-based tool: {tool_name}({tool_args})")
        result = execute_tool(
            tool_name, tool_args, brain.action_registry, brain.reminder_mgr,
            speak_fn=brain.speak_fn,
        )
        logger.info(f"Brain tool result: {result}")
        brain._record_trace_tool(tool_name, tool_args, result)
        results.append(result)

    # Build the final response
    if actions and results and not spoken:
        result_text = ". ".join(str(r) for r in results if r)
        spoken = result_text if result_text else "Done!"

    if not spoken:
        spoken = content

    spoken = brain._sanitize_response(spoken)
    brain.messages.append({"role": "assistant", "content": spoken})
    return spoken


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _validate_and_redirect(brain, content):
    """Detect when LLM gives instructions instead of using tools.

    If the LLM output contains step-by-step instructions for a task that
    should have been handled by a tool, execute the tool directly.

    Returns a response string if redirected, or None to let normal flow continue.
    """
    lower = content.lower()

    # Only redirect if the response looks like instructions/refusal
    _instruction_markers = [
        "here are the steps", "here's how", "you can ", "follow these",
        "open powershell", "open command prompt", "open terminal",
        "run the command", "type the command", "use the command",
        "run this command", "execute the command",
        "you can use powershell", "you can use cmd",
        "you could use", "try running",
    ]
    if not any(marker in lower for marker in _instruction_markers):
        return None

    # Check if user's original message was about system/process queries
    user_msg = ""
    for msg in reversed(brain.messages):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "").lower()
            break

    if not user_msg:
        return None

    # Map user intent → PowerShell command
    _REDIRECT_MAP = [
        (r"(?:list|show|what).*process", "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 Name,Id,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize"),
        (r"(?:disk|storage|drive)\s*(?:space|usage)?", "Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{N='Used(GB)';E={[math]::Round($_.Used/1GB,1)}},@{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}} | Format-Table -AutoSize"),
        (r"(?:cpu|processor)\s*(?:usage|load)?", "Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 1 | ForEach-Object { $_.CounterSamples | ForEach-Object { 'CPU: ' + [math]::Round($_.CookedValue,1).ToString() + '%' } }"),
        (r"(?:ram|memory)\s*(?:usage)?", "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 Name,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize"),
        (r"(?:ip|ip address|network)", "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike 'Loopback*' -and $_.IPAddress -ne '127.0.0.1' } | Select-Object InterfaceAlias,IPAddress | Format-Table -AutoSize"),
        (r"(?:battery)", "(Get-WmiObject Win32_Battery).EstimatedChargeRemaining"),
        (r"(?:port|listening)", "Get-NetTCPConnection -State Listen | Select-Object LocalPort,OwningProcess | Sort-Object LocalPort | Format-Table -AutoSize"),
        (r"(?:service|running service)", "Get-Service | Where-Object {$_.Status -eq 'Running'} | Select-Object Name,DisplayName | Format-Table -AutoSize"),
    ]

    for pattern, cmd in _REDIRECT_MAP:
        if re.search(pattern, user_msg):
            logger.info(f"Response validation: redirecting to run_terminal (LLM gave instructions)")
            try:
                from brain_defs import _run_terminal
                result = _run_terminal(cmd)
                if result:
                    brain.messages.append({"role": "assistant", "content": result})
                    return result
            except Exception as e:
                logger.error(f"Redirect execution failed: {e}")
            break

    return None


def _handle_extracted_tools(brain, extracted, content, round_num):
    """Handle tool calls extracted from text JSON."""
    from brain import execute_tool, log_action

    logger.info(f"Extracted {len(extracted)} tool(s) from text JSON")
    all_results = []
    for tool_name, tool_args in extracted:
        logger.info(f"Extracted tool call: {tool_name}({tool_args})")
        result = execute_tool(
            tool_name, tool_args, brain.action_registry,
            brain.reminder_mgr, speak_fn=brain.speak_fn,
        )
        logger.info(f"Extracted tool result: {result}")
        log_action("brain", tool_name, str(result)[:200],
                   "error" not in str(result).lower())
        all_results.append((tool_name, result))

    # Ask LLM to give a natural spoken response from results
    results_text = "; ".join(
        f"[Tool {name} returned: {res}]" for name, res in all_results
    )
    brain.messages.append({"role": "assistant", "content": content})
    brain.messages.append({
        "role": "user",
        "content": f"{results_text} Give the answer directly and naturally. "
                   f"NEVER mention tool names or say 'I used'. Just speak the result as a human would.",
    })
    followup = brain._call_llm_simple()
    if followup:
        spoken = brain._sanitize_response(followup.get("content", ""))
        if spoken:
            brain.messages.append({"role": "assistant", "content": spoken})
            return spoken

    # Fallback: summarize results directly
    summary = ". ".join(str(r) for _, r in all_results if r)
    return summary or "Done!"


def _handle_garbage_json(brain, extracted, content):
    """Handle tool calls extracted from garbage JSON text."""
    from brain import execute_tool, log_action

    logger.info(f"Extracted {len(extracted)} tool(s) from native garbage text")
    results = []
    for fn_name, fn_args in extracted:
        logger.info(f"Extracted tool call: {fn_name}({fn_args})")
        result = execute_tool(
            fn_name, fn_args, brain.action_registry, brain.reminder_mgr,
            speak_fn=brain.speak_fn,
        )
        logger.info(f"Extracted tool result: {result}")
        results.append(result)

    # Strip tool text from content for spoken response
    spoken = re.sub(r'```(?:json)?\s*\{.*?\}\s*```', '', content, flags=re.DOTALL)
    spoken = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', spoken)
    spoken = re.sub(r'\w+\s*\(.*?\)', '', spoken, flags=re.DOTALL)
    spoken = brain._sanitize_response(spoken.strip())
    if not spoken:
        spoken = ". ".join(str(r) for r in results if r) or "Done!"
    brain.messages.append({"role": "assistant", "content": spoken})
    return spoken


def _execute_single_tool(brain, tc, _seen_tools, round_num):
    """Execute a single tool call. Returns response str, "__CONTINUE__" to retry, or None."""
    from brain import execute_tool, log_action, _resolve_tool_name, _CORE_TOOL_NAMES

    fn_name = tc["function"]["name"]
    try:
        fn_args = json.loads(tc["function"]["arguments"])
    except (json.JSONDecodeError, KeyError, TypeError):
        fn_args = {}

    # Validate tool name
    resolved = _resolve_tool_name(fn_name)
    if not resolved:
        logger.warning(f"Brain hallucinated tool: {fn_name} (not in known tools)")
        brain.messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", f"call_{round_num}"),
            "content": f"Error: '{fn_name}' is not a valid tool. Use one of: {', '.join(sorted(_CORE_TOOL_NAMES))}",
        })
        return "__CONTINUE__"
    fn_name = resolved

    # Hallucination circuit breaker
    # For run_terminal, track tool+command (same tool with different commands is OK)
    call_key = fn_name
    if fn_name == "run_terminal":
        call_key = f"run_terminal:{fn_args.get('command', '')[:50]}"
    _seen_tools.append(call_key)
    if len(_seen_tools) >= 2:
        if _seen_tools[-1] == _seen_tools[-2]:
            logger.warning(f"Circuit breaker: LLM repeated {call_key}")
            return f"I tried {fn_name} but it didn't work. Could you rephrase your request?"
        if len(_seen_tools) >= 3 and _seen_tools[-1] == _seen_tools[-3]:
            logger.warning(f"Circuit breaker: LLM alternating {_seen_tools[-3:]}")
            return "I tried multiple approaches but couldn't complete the task. Could you rephrase?"

    logger.info(f"Brain tool call: {fn_name}({fn_args})")
    result = execute_tool(
        fn_name, fn_args, brain.action_registry, brain.reminder_mgr,
        speak_fn=brain.speak_fn,
    )
    logger.info(f"Brain tool result: {result}")
    brain._record_trace_tool(fn_name, fn_args, result)

    log_action("brain", fn_name, str(result)[:200],
               "error" not in str(result).lower())

    brain.messages.append({
        "role": "tool",
        "tool_call_id": tc.get("id", f"call_{round_num}"),
        "content": str(result) if result else "Done.",
    })
    return None  # Continue to next round


def _execute_parallel_tools(brain, tool_calls, round_num):
    """Execute multiple tool calls in parallel."""
    from brain import execute_tool, log_action, _resolve_tool_name

    def _exec_tool(tc):
        fn_name = tc["function"]["name"]
        try:
            fn_args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError, TypeError):
            fn_args = {}
        resolved = _resolve_tool_name(fn_name)
        if not resolved:
            logger.warning(f"Brain hallucinated tool (parallel): {fn_name}")
            return tc, f"Error: '{fn_name}' is not a valid tool."
        fn_name = resolved
        logger.info(f"Brain tool call (parallel): {fn_name}({fn_args})")
        result = execute_tool(
            fn_name, fn_args, brain.action_registry, brain.reminder_mgr,
            speak_fn=brain.speak_fn,
        )
        logger.info(f"Brain tool result: {result}")
        brain._record_trace_tool(fn_name, fn_args, result)
        log_action("brain", fn_name, str(result)[:200],
                   "error" not in str(result).lower())
        return tc, result

    # Ensure unique IDs
    for i, tc in enumerate(tool_calls):
        if not tc.get("id"):
            tc["id"] = f"call_{round_num}_{i}"

    with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
        futures = {pool.submit(_exec_tool, tc): tc for tc in tool_calls}
        results = {}
        for future in futures:
            try:
                tc, result = future.result(timeout=20)
                results[tc["id"]] = result
            except Exception as e:
                # Find which tc this future belongs to
                tc = futures[future]
                tc_id = tc.get("id", f"call_{round_num}")
                logger.warning(f"Parallel tool {tc['function']['name']} failed: {e}")
                results[tc_id] = f"Tool timed out or failed: {e}"

    # Append results in original order
    for tc in tool_calls:
        tc_id = tc["id"]
        result = results.get(tc_id, "Done.")
        brain.messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": str(result) if result else "Done.",
        })


def _handle_refusal_retry(brain, content):
    """Handle LLM refusal in prompt-based mode — retry with stronger instruction."""
    logger.info("Prompt-based LLM refused to use tools, retrying with correction")
    user_msg = ""
    for msg in reversed(brain.messages):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "")
            break

    if not user_msg:
        return content

    tool_hint = brain._suggest_tool_for_retry(user_msg)
    brain.messages.append({
        "role": "assistant",
        "content": "Let me use my tools to help with that."
    })
    brain.messages.append({
        "role": "user",
        "content": (
            f"You MUST use a tool for this. Output JSON like: "
            f'{{"actions": [{{"tool": "tool_name", "args": {{"param": "value"}}}}]}}\n'
            f"{tool_hint}"
            f"Now handle: {user_msg}"
        )
    })
    response = brain._call_llm_simple()
    if response:
        new_content = response.get("content", "")
        if new_content and not brain._is_llm_refusal(new_content):
            return new_content

    # Give up — clean up injected messages
    _pop_count = 0
    while (len(brain.messages) > 1 and _pop_count < 4
           and brain.messages[-1].get("role") != "user"):
        brain.messages.pop()
        _pop_count += 1
    if (brain.messages and
            brain.messages[-1].get("content", "").startswith("You MUST")):
        brain.messages.pop()
    if (brain.messages and
            brain.messages[-1].get("content") == "Let me use my tools to help with that."):
        brain.messages.pop()

    return content
