"""
Input classification for mode routing.

Extracted from: brain.py _QUICK_PATTERNS, _AGENT_PATTERNS, _DIRECT_TOOL_PATTERNS,
                Brain._classify_mode()

Responsibility:
  - Fast regex pattern matching for common commands
  - Direct tool name extraction from unambiguous inputs
  - LLM-based classification for ambiguous inputs
  - Returns ModeDecision with mode, confidence, and reason
"""

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ModeDecision:
    """Result of mode classification."""
    mode: str          # "quick", "agent", or "research"
    confidence: float  # 0.0 to 1.0
    reason: str        # Why this mode was chosen


# Patterns that are ALWAYS quick mode -- single tool, no screen interaction
QUICK_PATTERNS = [
    re.compile(r"^what('s| is) the (time|date|weather|temperature|forecast)", re.I),
    re.compile(r"^(get|tell|give) me the (time|date|weather|news)", re.I),
    re.compile(r"^(open|launch|close|minimize|maximize)\s+(?!.*\b(and|then)\b)[\w\s]{1,30}$", re.I),
    re.compile(r"^set (a |an )?reminder\b", re.I),
    re.compile(r"^(turn on|turn off|toggle|enable|disable)\s+(dark mode|bluetooth|wifi|wi-fi|night light|airplane)", re.I),
    re.compile(r"^(shutdown|restart|sleep|cancel shutdown)$", re.I),
    re.compile(r"^(pause|resume|next|previous|skip|volume|mute)\s*(music|song|track)?$", re.I),
    re.compile(r"^(hey|hi|hello|good (morning|afternoon|evening)|how are you|thanks?|thank you)", re.I),
    re.compile(r"(introduce yourself|tell .+ about yourself|who are you|what can you do|what are you|describe yourself)", re.I),
    # Terminal / system info queries
    re.compile(r"^(how much|check|what'?s? my) (disk|storage|ram|memory|cpu|battery)", re.I),
    re.compile(r"^(what'?s? my|show my|check my) (ip|network|system|processes)", re.I),
    # Software management
    re.compile(r"^(install|uninstall|update)\s+\w+", re.I),
    # File operations
    re.compile(r"^(move|copy|rename|delete|zip|unzip|find|list|organize)\s+", re.I),
]

# Patterns that indicate multi-step / screen-interactive tasks -> agent mode
AGENT_PATTERNS = [
    # Multi-step commands with connectors
    r".+\b(and then|then|after that|and also|and)\b\s+(open|play|search|click|type|go|show|create|close|send)",
    # Search in a specific app + play/interact
    r"search\b.+\b(in|on|using)\s+(spotify|youtube|chrome|firefox|edge|browser)",
    r"(search|look)\s+(for\s+)?.+\s+on\s+(youtube|spotify)\s+and\s+(play|open|watch)",
    # Play music/video with context — needs UI interaction to complete
    r"(play|listen to|watch|put on)\s+.{1,50}\s+(on|in)\s+(youtube|spotify)\s+and\s+(play|watch|listen)",
    # YouTube-specific: "search X on YouTube and play a video"
    r"(search|find|look for)\s+.+\s+on\s+youtube",
    # Form/navigation tasks
    r"\b(fill out|fill in|complete the form|submit the form)\b",
    r"\bnavigate to .+ and (click|fill|submit|select|type)\b",
    # UI interaction keywords
    r"\b(click|click on|press the button|tap on|select the|drag|scroll to|find the button)\b",
    # Multi-app tasks
    r"(copy|take|grab|get)\b.+\b(from|in)\s+\w+.+\b(paste|put|send|save|move)\b.+\b(to|in|into)\s+\w+",
    # Show/demonstrate/preview results
    r"(show|demonstrate|preview|display|open)\s+(it|that|the result|the file|what you (made|created))",
    # Go to / navigate specific page/section in app
    r"go to\b.+\b(in|on)\s+(chrome|firefox|edge|browser|spotify|settings)",
    # Download / install tasks
    r"\b(download|install)\b.+\b(from|on)\s+",
    # Log in / sign in
    r"\b(log in|sign in|login|signin)\b.+\b(to|on|into)\b",
    # Window arrangement
    r"snap\b.+\b(left|right|top|bottom)",
    r"arrange\b.+\bwindows",
    r"close all\b.+\bexcept",
    # Screen context actions
    r"(save|export)\s+this\s+(page|tab)\s+as\s+pdf",
    r"run\s+this\s+(file|script|code)",
    r"open\s+terminal\s+here",
]

# Direct tool patterns -- skip mode classification entirely for unambiguous requests
DIRECT_TOOL_PATTERNS = [
    (re.compile(r"(disk space|how much (ram|memory|cpu|storage)|my ip|ping |tracert|tasklist|git |docker |npm |pip |node |whoami)", re.I), "run_terminal"),
    (re.compile(r"^(install|uninstall|update)\s+\w+$", re.I), "manage_software"),
    (re.compile(r"(move|copy|rename|delete|zip|find)\s+.*(file|folder|pdf|doc|screenshot|png|jpg|zip)", re.I), "manage_files"),
    (re.compile(r"(create|make|build|generate|write)\s+(a |an |me )?(simple |basic |beautiful )?(calculator|page|website|html|script|file|document|app|application|program|game|form|landing)", re.I), "create_file"),
    (re.compile(r"(play|listen to)\s+(some |a |the |my )?(good |best |romantic |awesome |sad |soft |hard |classic )?(music|song|songs|track|tracks|playlist|album|rock|jazz|pop|blues|country|hip.?hop|rap|metal|classical|lo.?fi|chill|edm)", re.I), "play_music"),
    (re.compile(r"(play|listen to)\s+.{1,50}\s+(on|in|using|with)\s+(spotify|youtube)", re.I), "play_music"),
    # Browser actions
    (re.compile(r"(go to|navigate to|open)\s+https?://", re.I), "browser_action"),
    (re.compile(r"(go to|navigate to|open)\s+www\.", re.I), "browser_action"),
    (re.compile(r"(read|get|show)\s+(this|the|current)\s+(page|webpage|website|tab)", re.I), "browser_action"),
]


def classify_mode(user_input, quick_chat_fn=None):
    """Classify request into quick/agent/research mode.

    Uses fast regex heuristics first, then LLM classification for ambiguous cases.

    Args:
        user_input: User's text input.
        quick_chat_fn: Optional callable for LLM-based classification
                       (for ambiguous cases). Signature: fn(prompt) -> str.

    Returns:
        ModeDecision with mode ("quick", "agent", or "research"),
        confidence (0.0-1.0), and reason string.
    """
    try:
        from core.metrics import metrics
        _timer = metrics.timer("mode_classification")
        _timer.__enter__()
    except Exception:
        _timer = None

    def _finish(decision):
        if _timer:
            try:
                _timer.__exit__(None, None, None)
            except Exception:
                pass
        return decision

    lower = user_input.lower().strip()

    # ---- PRE-CLASSIFICATION: direct tool pattern match (fastest) ----
    for pattern, tool_name in DIRECT_TOOL_PATTERNS:
        if pattern.search(lower):
            logger.info(f"Direct tool shortcut: {tool_name}")
            return _finish(ModeDecision("quick", 1.0, f"direct tool pattern: {tool_name}"))

    # ---- QUICK MODE: obvious single-tool requests (fast exit) ----
    if any(p.search(lower) for p in QUICK_PATTERNS):
        return _finish(ModeDecision("quick", 0.95, "quick pattern match"))

    # ---- RESEARCH MODE: questions needing multi-source web research ----
    _tool_answerable = r"\b(weather|forecast|temperature|time|date|reminder|app|file|disk|ram|cpu|battery|software|install)\b"
    if re.search(_tool_answerable, lower):
        pass
    else:
        action_verbs = r"^(create|open|play|launch|close|minimize|send|set|turn|toggle|make)\b"
        if not re.search(action_verbs, lower):
            research_triggers = [
                r"\bcompare\b", r"\bvs\b", r"\bversus\b",
                r"difference between", r"pros and cons",
                r"\bresearch\b", r"\binvestigate\b",
                r"find the best\b", r"find me the best\b",
                r"what are the top\b", r"\brecommend\b",
                r"best .+ for\b", r"which .+ should i\b",
                r"deep dive into\b", r"tell me everything about\b",
                r"explain .+ in detail\b",
                r"\bhistory of\b", r"\bevolution of\b",
                r"search for .+(history|overview|guide|tutorial)\b",
            ]
            for pattern in research_triggers:
                if re.search(pattern, lower):
                    return _finish(ModeDecision("research", 0.85, f"research trigger: {pattern[:50]}"))

    # ---- SMART DECOMPOSITION: "X and Y" where both have dedicated tools ----
    compound = re.match(r"^(open|launch)\s+(\w+)\s+and\s+(play|search|find)\s+(.+)$", lower)
    if compound:
        logger.info("Smart decomposition: compound request with dedicated tools -> quick")
        return _finish(ModeDecision("quick", 0.9, "smart decomposition: compound request"))

    create_and_show = re.search(r"(create|make|build|write)\s+.+and\s+(show|open|display|preview)", lower)
    if create_and_show:
        logger.info("Smart decomposition: create+show -> quick (create_file auto-opens)")
        return _finish(ModeDecision("quick", 0.9, "smart decomposition: create+show"))

    # ---- AGENT MODE: multi-step tasks needing screen interaction ----
    for pattern in AGENT_PATTERNS:
        if re.search(pattern, lower, re.I):
            logger.info(f"Agent mode triggered by pattern: {pattern[:50]}")
            return _finish(ModeDecision("agent", 0.9, f"agent pattern: {pattern[:50]}"))

    # ---- LLM CLASSIFICATION: ambiguous cases ----
    words = lower.split()
    has_action = re.search(r"\b(create|make|build|play|search|find|go|download|install|open .+ and)\b", lower)
    if len(words) >= 5 and has_action and quick_chat_fn:
        try:
            llm_answer = quick_chat_fn(
                f"Does this task need MULTIPLE steps on a computer screen "
                f"(opening apps, clicking buttons, typing in search bars, etc.)? "
                f"Task: \"{user_input}\"\n"
                f"Answer ONLY 'yes' or 'no'."
            )
            if llm_answer and "yes" in llm_answer.lower()[:10]:
                logger.info(f"Agent mode triggered by LLM classification")
                return _finish(ModeDecision("agent", 0.7, "LLM classified as agent"))
        except Exception:
            pass

    # ---- DEFAULT: quick mode ----
    return _finish(ModeDecision("quick", 0.5, "default quick mode"))
