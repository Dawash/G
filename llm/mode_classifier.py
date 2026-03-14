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
    re.compile(r"^what (time|date) is it", re.I),
    re.compile(r"^(get|tell|give) me the (time|date|weather|news)", re.I),
    re.compile(r"^(open|launch|close|minimize|maximize)\s+(?!.*\b(and|then)\b)[\w\s]{1,30}$", re.I),
    re.compile(r"^set (a |an )?reminder\b", re.I),
    re.compile(r"^(turn on|turn off|toggle|enable|disable)\s+(dark mode|bluetooth|wifi|wi-fi|night light|airplane)", re.I),
    re.compile(r"^(shutdown|restart|sleep|cancel shutdown)$", re.I),
    re.compile(r"^(pause|resume|next|previous|skip|volume|mute|unmute)\s*(up|down|music|song|track)?$", re.I),
    re.compile(r"^(hey|hi|hello|good (morning|afternoon|evening)|how are you|thanks?|thank you)\b", re.I),
    re.compile(r"(introduce yourself|tell .+ about yourself|who are you|what can you do|what are you|describe yourself)", re.I),
    # Terminal / system info queries
    re.compile(r"^(how much|check( my)?|what'?s? my) (disk|storage|ram|memory|cpu|battery)\b", re.I),
    re.compile(r"^(what'?s? my|show my|check my) (ip|ip address|network|system|processes)\b", re.I),
    # Software management
    re.compile(r"^(install|uninstall|update)\s+\w+", re.I),
    # File operations — "find" only for files/folders, not "find the best X"
    re.compile(r"^(move|copy|rename|delete|zip|unzip|list|organize)\s+", re.I),
    re.compile(r"^find\s+(?!the best|me the|the top|a good)(?:file|folder|document|pdf|doc|image|photo|screenshot)\b", re.I),
]

# Patterns that indicate multi-step / screen-interactive tasks -> agent mode
AGENT_PATTERNS = [
    # Multi-step commands with connectors
    r".+\b(and then|then|after that|and also|and)\b\s+(open|play|search|click|type|go|show|create|close|send)",
    # Search in a specific app + play/interact
    r"search\b.+\b(in|on|using)\s+(spotify|youtube|chrome|firefox|edge|browser)",
    r"(search|look)\s+(for\s+)?.+\s+on\s+(youtube|spotify)\s+and\s+(play|open|watch)",
    # Play music/video on apps — requires UI interaction (search, click results, verify playback)
    r"(play|listen to|watch|put on)\s+.{1,50}\s+(on|in|using|with)\s+(youtube|spotify)",
    r"(play|listen to|watch|put on)\s+(some |a |the |my )?(good |best |romantic |awesome |sad |soft )?(music|song|songs|track|video)\s+(on|in|using|with)\s+(youtube|spotify)",
    # Spotify/YouTube interaction — always needs agent for UI automation
    r"(search|find|look for|play)\s+.+\s+on\s+(youtube|spotify)",
    r"(open|launch)\s+(youtube|spotify)\s+and\s+(play|search|find|watch|listen)",
    # Generic "play X on Y" — needs agent to handle search + click + verify
    r"(play|listen to)\s+.+\s+(on|in)\s+(spotify|youtube)",
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
    # Go to / navigate and then do something
    r"(?:go to|navigate to|visit)\s+\w+.+\band\s+(?:upvote|downvote|like|comment|post|reply|click|share|subscribe|follow|watch|read|check|vote)",
    r"\bnavigate to\b.+\band\s+(?:turn|enable|disable|change|set|toggle|click|select)",
    # Download / install tasks
    r"\b(download|install)\b.+\b(from|on)\s+",
    # Log in / sign in
    r"\b(log ?in(?:to)?|sign ?in(?:to)?|login|signin)\b.+\b(to|on|into|my|the)?\b",
    # Window arrangement
    r"snap\b.+\b(left|right|top|bottom)",
    r"arrange\b.+\bwindows",
    r"close all\b.+\bexcept",
    # Screen context actions
    r"(save|export)\s+this\s+(page|tab)\s+as\s+pdf",
    r"run\s+this\s+(file|script|code)",
    r"open\s+terminal\s+here",
    # Order/shop/book — requires web UI interaction
    # Exclude "remind me to buy X at Y" (that's a reminder, not shopping)
    r"(?<!remind me to )(?<!reminder to )\b(order|book|buy|purchase|shop for)\b.+\b(online|from|on|at)\b",
    r"(?<!remind me to )\b(order|book|buy|purchase)\b.+\b(pizza|food|ticket|flight|hotel|uber|lyft)\b",
]

# Direct tool patterns -- skip mode classification entirely for unambiguous requests
DIRECT_TOOL_PATTERNS = [
    (re.compile(r"(disk space|how much (ram|memory|cpu|storage) (do i|does|is)|check my (ram|memory|cpu|disk|storage|battery|ip)|^my ip$|^my ip address$|^ping \w|^tracert \w|^tasklist$|^git \w|^docker \w|^npm \w|^pip \w|^node \w|^whoami$)", re.I), "run_terminal"),
    (re.compile(r"^(install|uninstall|update)\s+\w+$", re.I), "manage_software"),
    (re.compile(r"(move|copy|rename|delete|zip|find)\s+.*(file|folder|pdf|doc|screenshot|png|jpg|zip)", re.I), "manage_files"),
    (re.compile(r"(create|make|build|generate|write)\s+(a |an |me )?(simple |basic |beautiful )?(calculator|page|website|html|script|file|document|app|application|program|game|form|landing)", re.I), "create_file"),
    # Generic music without specifying app — quick mode play_music handles media keys
    # ^anchor ensures "open youtube and play jazz" doesn't match (→ agent mode instead)
    (re.compile(r"^(play|listen to|put on)\s+(some |a |the |my )?(good |best |romantic |awesome |sad |soft |hard |classic |chill )?(music|song|songs|track|tracks|playlist|album|rock|jazz|pop|blues|country|hip.?hop|rap|metal|classical|lo.?fi|chill|edm)$", re.I), "play_music"),
    # NOTE: "play X on spotify/youtube" intentionally NOT here — routed to agent mode
    # for proper UI interaction (search → click result → verify playback)
    # Screenshot
    (re.compile(r"(take|capture|grab|save)\s+(a\s+)?screenshot", re.I), "take_screenshot"),
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

    # ---- COMPOUND ACTION PRE-CHECK: "X and Y" where Y is an action → agent ----
    # Must run BEFORE direct tool patterns, which would match only the first part
    if re.search(r'\band\s+(?:then\s+)?(?:send|email|post|upload|save|share|book|order|buy|play|search|open|go|navigate|click|type|fill|create|make|write|download|install|take|capture|paste|compose|reply|forward|calculate|check|browse|watch|listen|read|run|start|launch|close|delete|move|copy|rename)\b', lower):
        # Confirm it's truly compound (has a preceding action/object, not just "and open X")
        if re.search(r'(?:^|\s)(?:\w+\s+){2,}and\s+', lower):
            logger.info("Agent mode: compound action detected (X and Y)")
            return _finish(ModeDecision("agent", 0.9, "compound action: X and Y"))

    # ---- APP-SPECIFIC INTERACTIONS: "X on/in <app>" or "search <site> for Y" ----
    if re.search(r'\b(?:send|compose|write|post)\s+.+\b(?:on|in|via|using|through)\s+(?:whatsapp|telegram|slack|discord|messenger|teams|twitter|instagram|facebook|reddit)\b', lower):
        return _finish(ModeDecision("agent", 0.9, "messaging/social app interaction"))
    if re.search(r'\bsearch\s+(?:for\s+)?(?:.+\s+on\s+)?(?:amazon|ebay|flipkart|walmart|etsy|aliexpress)\b', lower):
        return _finish(ModeDecision("agent", 0.9, "site-specific search"))
    if re.search(r'\b(?:search|look)\s+(?:for\s+)?.+\s+on\s+(?:amazon|ebay|flipkart|walmart|etsy)\b', lower):
        return _finish(ModeDecision("agent", 0.9, "site-specific search"))

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
                # Knowledge questions needing multi-source research (6+ words)
                # Short factual questions ("what is the capital of france") go to quick_chat
                r"^what (?:is|are) (?!the (?:time|date|weather|temperature|forecast|capital))\w.{25,}",
                r"^(?:how|why) (?:does|do|is|are|did|can|could|would|should) .{25,}",
            ]
            for pattern in research_triggers:
                if re.search(pattern, lower):
                    return _finish(ModeDecision("research", 0.85, f"research trigger: {pattern[:50]}"))

    # ---- SMART DECOMPOSITION: "X and Y" where both have dedicated tools ----
    # BUT: "open spotify/youtube and play/search" → agent mode (needs UI interaction)
    compound = re.match(r"^(open|launch)\s+(\w+)\s+and\s+(play|search|find)\s+(.+)$", lower)
    if compound:
        compound_app = compound.group(2).lower()
        if compound_app in ("spotify", "youtube", "chrome", "firefox", "edge", "browser"):
            logger.info(f"Smart decomposition: {compound_app} + interactive action -> agent mode")
            return _finish(ModeDecision("agent", 0.9, f"smart decomposition: {compound_app} interactive"))
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

    # ---- FUZZY KEYWORD MATCHING: catch common speech-to-text typos ----
    from difflib import SequenceMatcher
    _words = lower.split()
    _AGENT_KEYWORDS = ["autonomous", "automate", "automatic", "agent"]
    _RESEARCH_KEYWORDS = ["research", "investigate", "analyze", "study"]
    # Common words that should NOT fuzzy-match into different categories
    _FUZZY_EXCLUDE = {"search", "find", "start", "state", "stage", "style", "store", "study"}
    for word in _words:
        if len(word) >= 5 and word not in _FUZZY_EXCLUDE:
            for kw in _AGENT_KEYWORDS:
                if SequenceMatcher(None, word, kw).ratio() > 0.80:
                    return _finish(ModeDecision("agent", 0.85, f"fuzzy match: {word}→{kw}"))
            for kw in _RESEARCH_KEYWORDS:
                if SequenceMatcher(None, word, kw).ratio() > 0.80:
                    return _finish(ModeDecision("research", 0.85, f"fuzzy match: {word}→{kw}"))

    # ---- DEFAULT: quick mode ----
    return _finish(ModeDecision("quick", 0.5, "default quick mode"))
