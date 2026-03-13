"""
Execution Strategy Selector — multi-layer task execution routing.

Routes each task step to the fastest reliable execution method:
  1. CLI    — PowerShell/CMD for system operations (0.5s)
  2. API    — Direct service API calls (1-2s)
  3. TOOL   — Brain tools like open_app, get_weather (0.5s)
  4. UIA    — Windows Accessibility tree for desktop apps (0.2s)
  5. CDP    — Chrome DevTools Protocol for browser (1s)
  6. VISION — Screenshot + LLM analysis (5-10s, last resort)

Used by desktop_agent.py and brain.py to pick the optimal execution path.
"""

import logging
import os
import re
import subprocess
import json
import time

logger = logging.getLogger(__name__)

# Strategy constants
STRATEGY_CLI = "cli"
STRATEGY_API = "api"
STRATEGY_TOOL = "tool"
STRATEGY_UIA = "uia"
STRATEGY_CDP = "cdp"
STRATEGY_VISION = "vision"

# Priority order (fastest → slowest)
STRATEGY_ORDER = [STRATEGY_CLI, STRATEGY_API, STRATEGY_TOOL, STRATEGY_UIA, STRATEGY_CDP, STRATEGY_VISION]


# ===================================================================
# CLI Strategy: PowerShell/CMD commands for system tasks
# ===================================================================

# Safety: never run these via CLI
_CLI_BLOCKED = {"format", "del /s", "rm -rf", "remove-item c:", "remove-item /",
                "reg delete", "shutdown", "restart", "stop-computer"}

def _ram_per_app_cmd(app_name):
    """Generate PowerShell command to check RAM usage for a specific app."""
    return (
        f"Get-Process -Name '*{app_name}*' -ErrorAction SilentlyContinue | "
        "Measure-Object WorkingSet64 -Sum | ForEach-Object { "
        "$mb = [math]::Round($_.Sum/1MB); "
        "if($mb -ge 1024) { "
        f"'{app_name} is using ' + [math]::Round($mb/1024,1).ToString() + ' GB of RAM' "
        "} else { "
        f"'{app_name} is using ' + $mb.ToString() + ' MB of RAM' "
        "} }"
    )


_CLI_COMMANDS = [
    # Software management (winget)
    (r"\b(?:install|setup)\s+(.+?)(?:\s+app|\s+program)?$",
     lambda m: f'winget install --accept-source-agreements --accept-package-agreements "{m.group(1).strip()}"'),
    (r"\buninstall\s+(.+?)(?:\s+app|\s+program)?$",
     lambda m: f'winget uninstall "{m.group(1).strip()}"'),
    (r"\bupdate\s+(?:all|everything|apps?|software)",
     lambda m: "winget upgrade --all --accept-source-agreements"),
    (r"\blist\s+installed\s+(?:apps?|programs?|software)",
     lambda m: "winget list"),

    # System info
    (r"\bdisk\s*(?:space|usage|free|size)",
     lambda m: "Get-PSDrive C | ForEach-Object { $used = [math]::Round($_.Used/1GB,1); $free = [math]::Round($_.Free/1GB,1); $total = [math]::Round(($_.Used + $_.Free)/1GB,1); $pct = [math]::Round($used/$total*100); \"Drive C: $free GB free out of $total GB ($pct% used).\" }"),
    (r"\b(?:system|computer|pc)\s*(?:info|specs?)|(?:about|specs?\s*(?:of)?)\s+(?:my\s+)?(?:system|computer|pc|laptop)|(?:tell|show|what(?:'s)?)\s+(?:me\s+)?(?:about\s+)?(?:my\s+)?(?:computer|pc|laptop|system)\s*(?:info|specs?|details)?|(?:my\s+)?(?:pc|computer|laptop)\s+specs?",
     lambda m: "$os = Get-CimInstance Win32_OperatingSystem; $cpu = Get-CimInstance Win32_Processor; $ram = [math]::Round($os.TotalVisibleMemorySize/1MB); \"$($os.Caption) | $($cpu.Name) | $ram GB RAM | $($os.OSArchitecture)\""),
    (r"(?:what(?:'s)?\s+(?:is\s+)?my\s+)?(?:ip|network)\s*(?:address|info|config)|(?:my\s+ip)",
     lambda m: "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' } | Select-Object -First 1).IPAddress"),
    (r"\bwho\s*(?:am\s*i|logged\s*in)|username",
     lambda m: "$env:USERNAME"),
    (r"\bbattery\s*(?:level|status|percent)?",
     lambda m: "(Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | Format-List) 2>$null; if(!$?) { 'No battery detected (desktop PC)' }"),
    (r"\b(?:cpu|processor)\s*(?:usage|load|percent)",
     lambda m: "Get-CimInstance Win32_Processor | ForEach-Object { \"CPU is at $($_.LoadPercentage)% load.\" }"),
    (r"\b(?:ram|memory)\s*(?:usage|free|available|status|info)",
     lambda m: "Get-CimInstance Win32_OperatingSystem | ForEach-Object { $total = [math]::Round($_.TotalVisibleMemorySize/1MB,1); $free = [math]::Round($_.FreePhysicalMemory/1MB,1); $used = [math]::Round($total - $free, 1); \"You have $total GB total RAM. $used GB is in use, $free GB is free.\" }"),
    # "how much ram am I using", "how much memory is being used"
    (r"\bhow\s+much\s+(?:ram|memory)\s+(?:am\s+i|is\s+(?:being\s+)?)\s*(?:using|used|consumed)",
     lambda m: "Get-CimInstance Win32_OperatingSystem | ForEach-Object { $total = [math]::Round($_.TotalVisibleMemorySize/1MB,1); $free = [math]::Round($_.FreePhysicalMemory/1MB,1); $used = [math]::Round($total - $free, 1); $pct = [math]::Round($used/$total*100); \"You're using $used GB out of $total GB of RAM ($pct% used).\" }"),

    # Targeted "what app uses most RAM/CPU" queries — catches many natural phrasings
    # Matches: "which app is using most ram", "what's eating all my ram",
    #          "which app is eating up all my ram", "what's taking too much memory"
    (r"(?:what|which|what's)\s+(?:app|program|process)?\s*(?:is\s+)?(?:using|eating|consuming|hogging|taking)\s+(?:up\s+)?(?:(?:all|too)\s+(?:much\s+)?(?:of\s+)?(?:my\s+)?)?(?:the\s+)?(?:most\s+)?(?:\w+\s+){0,10}(?:ram|memory)",
     lambda m: "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 1 Name,@{N='RAM_GB';E={[math]::Round($_.WorkingSet64/1GB,1)}} | ForEach-Object { \"$($_.Name) is using $($_.RAM_GB) GB of RAM\" }"),
    (r"(?:what|which|what's)\s+(?:app|program|process)?\s*(?:is\s+)?(?:using|eating|consuming|hogging|taking)\s+(?:up\s+)?(?:(?:all|too)\s+(?:much\s+)?(?:of\s+)?(?:my\s+)?)?(?:the\s+)?(?:most\s+)?(?:\w+\s+){0,10}(?:cpu|processor)",
     lambda m: "Get-Process | Sort-Object CPU -Descending | Select-Object -First 1 Name,@{N='CPU_Sec';E={[math]::Round($_.CPU,1)}} | ForEach-Object { \"$($_.Name) has used $($_.CPU_Sec) seconds of CPU time\" }"),
    # "top N apps by ram/cpu" — returns ranked list in human-readable format
    # Also matches: "what are the top processes using CPU", "show top apps by memory"
    (r"(?:what\s+are\s+)?(?:the\s+)?(?:top|heaviest|biggest)\s+(?:\d+\s+)?(?:apps?|programs?|processes?)\s+(?:by|using|consuming)\s+(?:the\s+)?(?:most\s+)?(?:ram|memory)",
     lambda m: "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 Name,@{N='RAM';E={if($_.WorkingSet64 -ge 1GB){'{0:N1} GB' -f ($_.WorkingSet64/1GB)}else{'{0:N0} MB' -f ($_.WorkingSet64/1MB)}}} | Format-Table -AutoSize"),
    (r"(?:what\s+are\s+)?(?:the\s+)?(?:top|heaviest|biggest)\s+(?:\d+\s+)?(?:apps?|programs?|processes?)\s+(?:by|using|consuming)\s+(?:the\s+)?(?:most\s+)?(?:cpu|processor)",
     lambda m: "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,@{N='CPU(s)';E={[math]::Round($_.CPU,1)}},@{N='RAM';E={if($_.WS -ge 1GB){'{0:N1} GB' -f ($_.WS/1GB)}else{'{0:N0} MB' -f ($_.WS/1MB)}}} | Format-Table -AutoSize"),
    # "how much ram/cpu is X using"
    (r"\bhow\s+much\s+(?:ram|memory)\s+(?:is|does)\s+(.+?)\s+(?:using|use|consume|take)",
     lambda m: _ram_per_app_cmd(m.group(1).strip())),

    (r"\buptime|how\s*long\s*(?:\w+\s+){0,5}(?:been\s+)?(?:on|running|up)",
     lambda m: "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime | ForEach-Object { $d = $_.Days; $h = $_.Hours; $m = $_.Minutes; if($d -gt 0){\"Your PC has been on for $d days, $h hours and $m minutes.\"}elseif($h -gt 0){\"Your PC has been on for $h hours and $m minutes.\"}else{\"Your PC has been on for $m minutes.\"} }"),

    # Windows version
    (r"\b(?:windows|win)\s*(?:version|build|edition)|(?:what|which)\s+(?:version\s+(?:of\s+)?)?windows|(?:my\s+)?(?:os|windows)\s+version",
     lambda m: "(Get-CimInstance Win32_OperatingSystem | ForEach-Object { \"$($_.Caption) Build $($_.BuildNumber)\" })"),
    # Process count
    (r"\bhow\s+many\s+(?:process(?:es)?|apps?|programs?)\s+(?:are\s+)?(?:running|active|open)",
     lambda m: "(Get-Process).Count.ToString() + ' processes are currently running.'"),

    # Process management
    (r"\bkill\s+(.+?)(?:\s+process)?$",
     lambda m: f"Stop-Process -Name '{m.group(1).strip()}' -Force -ErrorAction SilentlyContinue; 'Killed {m.group(1).strip()}'"),
    (r"\b(?:list|show|running)\s*(?:process(?:es)?|apps?|programs?)",
     lambda m: "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 20 Name,@{N='CPU(s)';E={[math]::Round($_.CPU,1)}},@{N='RAM(MB)';E={[math]::Round($_.WS/1MB)}} | Format-Table -AutoSize"),

    # Network
    (r"\bwifi\s*(?:connect|join)\s+(.+)",
     lambda m: f'netsh wlan connect name="{m.group(1).strip()}"'),
    (r"\bwifi\s*(?:disconnect|off)|(?:turn|switch)\s+off\s+(?:the\s+)?wi-?fi|(?:disable)\s+wi-?fi",
     lambda m: "netsh wlan disconnect; 'WiFi disconnected'"),
    (r"(?:turn|switch)\s+on\s+(?:the\s+)?wi-?fi|(?:enable)\s+wi-?fi",
     lambda m: "netsh interface set interface Wi-Fi enable; 'WiFi enabled'"),
    (r"\bwifi\s*(?:list|scan|available|networks?)",
     lambda m: "netsh wlan show networks mode=bssid"),
    (r"(?:show|list|what(?:'s)?|check)\s+(?:me\s+)?(?:my\s+)?(?:network|internet)\s*(?:connections?|interfaces?|adapters?|status)",
     lambda m: "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { $ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress; \"$($_.Name): $($_.LinkSpeed) - IP: $ip\" }"),
    (r"\bping\s+(\S+)",
     lambda m: f"ping -n 4 {m.group(1).strip()}"),
    (r"\bflush\s*dns|clear\s*dns",
     lambda m: "Clear-DnsClientCache; 'DNS cache flushed'"),
    (r"\bpublic\s*ip",
     lambda m: "(Invoke-WebRequest -Uri 'https://api.ipify.org' -UseBasicParsing).Content"),

    # Windows settings via PowerShell
    (r"\bdark\s*mode\s*(?:on|enable)|(?:turn|switch|enable)\s+(?:on\s+)?dark\s*mode",
     lambda m: 'Set-ItemProperty -Path "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize" -Name "AppsUseLightTheme" -Value 0 -Force; Set-ItemProperty -Path "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize" -Name "SystemUsesLightTheme" -Value 0 -Force; "Dark mode enabled"'),
    (r"\bdark\s*mode\s*(?:off|disable)|light\s*mode\s*(?:on|enable)|(?:turn|switch|enable)\s+(?:on\s+)?light\s*mode|(?:turn|switch|disable)\s+(?:off\s+)?dark\s*mode",
     lambda m: 'Set-ItemProperty -Path "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize" -Name "AppsUseLightTheme" -Value 1 -Force; Set-ItemProperty -Path "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize" -Name "SystemUsesLightTheme" -Value 1 -Force; "Light mode enabled"'),
    (r"\bnight\s*light\s*(?:on|enable)",
     lambda m: 'Start-Process ms-settings:nightlight; Start-Sleep 1; "Night light settings opened — toggle it on"'),
    (r"\bshow\s*(?:desktop|minimize\s*all)|minimize\s+all(?:\s+(?:apps?|windows?|programs?))?(?:\s+(?:opened|open|running))?(?:\s+(?:right\s+)?now)?",
     lambda m: '(New-Object -ComObject Shell.Application).MinimizeAll(); "All windows minimized"'),
    # Bluetooth toggle
    (r"(?:turn|switch)\s+(?:off|on)\s+bluetooth|bluetooth\s+(?:off|on|disable|enable)|(?:disable|enable)\s+bluetooth",
     lambda m: ('$radios = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]::GetRadiosAsync().GetAwaiter().GetResult(); '
                '$bt = $radios | Where-Object { $_.Kind -eq "Bluetooth" }; '
                + ('$bt.SetStateAsync("Off").GetAwaiter().GetResult(); "Bluetooth turned off"'
                   if any(w in m.group(0) for w in ['off', 'disable'])
                   else '$bt.SetStateAsync("On").GetAwaiter().GetResult(); "Bluetooth turned on"'))),

    # File system
    (r"\b(?:empty|clear)\s*(?:recycle\s*bin|trash)",
     lambda m: "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'Recycle bin emptied'"),
    (r"\b(?:create|make|new)\s*(?:folder|directory)\s+(.+)",
     lambda m: f"New-Item -ItemType Directory -Path '{m.group(1).strip()}' -Force | Select-Object FullName"),
    (r"\bfolder\s*size\s+(.+)",
     lambda m: f"(Get-ChildItem '{m.group(1).strip()}' -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB | ForEach-Object {{ '{0:N1} MB' -f $_ }}"),
    (r"\btemp\s*(?:files?)?\s*(?:clean|clear|delete|remove)|(?:clean|clear|delete|remove)\s+(?:up\s+)?(?:my\s+)?temp(?:orary)?\s*(?:files?)?",
     lambda m: "$before = (Get-ChildItem $env:TEMP -Recurse -ErrorAction SilentlyContinue | Measure-Object).Count; Remove-Item $env:TEMP\\* -Recurse -Force -ErrorAction SilentlyContinue; $after = (Get-ChildItem $env:TEMP -Recurse -ErrorAction SilentlyContinue | Measure-Object).Count; \"Cleaned $($before - $after) temp files\""),

    # Clipboard
    (r"\bclipboard\s*(?:content|text|show|get)",
     lambda m: "Get-Clipboard"),
    (r"\bcopy\s+(?:text\s+)?['\"](.+?)['\"](?:\s+to\s+clipboard)?",
     lambda m: f"Set-Clipboard -Value '{m.group(1)}'; 'Copied to clipboard'"),

    # Environment
    (r"\benv(?:ironment)?\s*(?:var(?:iable)?s?)\s*(?:list|show)?",
     lambda m: "Get-ChildItem Env: | Sort-Object Name | Select-Object -First 30 Name,Value | Format-Table -AutoSize"),
    (r"\bpython\s*version",
     lambda m: "python --version"),
    (r"\bnode\s*version",
     lambda m: "node --version"),
    (r"\bgit\s*version",
     lambda m: "git --version"),
]


def match_cli_command(text):
    """Check if text can be handled via CLI. Returns PowerShell command or None."""
    lower = text.lower().strip()
    # Safety check
    for blocked in _CLI_BLOCKED:
        if blocked in lower:
            return None
    for pattern, cmd_fn in _CLI_COMMANDS:
        m = re.search(pattern, lower)
        if m:
            return cmd_fn(m)
    return None


def execute_cli(command, timeout=30):
    """Execute a PowerShell command and return result."""
    # Final safety check
    cmd_lower = command.lower()
    for blocked in _CLI_BLOCKED:
        if blocked in cmd_lower:
            return f"Blocked for safety: {command[:60]}"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            err = result.stderr.strip()
            # Filter out common non-errors
            if "ProgressPreference" not in err and "WARNING" not in err:
                return f"Error: {err[:300]}"
        return output[:2000] if output else "Command completed successfully."
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except FileNotFoundError:
        return "PowerShell not found."
    except Exception as e:
        return f"CLI error: {e}"


# ===================================================================
# API Strategy: Direct service API calls (bypass UI entirely)
# ===================================================================

_API_HANDLERS = {}


def register_api(service_name):
    """Decorator to register an API handler."""
    def decorator(fn):
        _API_HANDLERS[service_name] = fn
        return fn
    return decorator


def match_api_service(text):
    """Check if text maps to a direct API. Returns (service, params) or None."""
    lower = text.lower().strip()

    # Spotify: play music (catches "play a good song", "play X on spotify", etc.)
    # Skip if intent is UI interaction (click/press/find/scroll) rather than music playback
    is_ui_action = re.search(r"^(?:click|press|tap|find|scroll|select|check|toggle)\b", lower)
    if not is_ui_action and (
        re.search(r"\bplay\s+.+?\s+(?:on|in)\s+spotify", lower) or
        re.search(r"\bplay\s+(?:some\s+|a\s+|the\s+)?(?:good\s+|nice\s+|chill\s+)?(?:music|song|track)", lower)
    ):
        query = ""
        for pat in [r"play\s+(.+?)\s+(?:on|in)\s+spotify",
                    r"play\s+(.+?)$"]:
            m = re.search(pat, lower)
            if m:
                q = m.group(1).strip()
                q = re.sub(r'^(a |an |some |the |any |or |me )', '', q).strip()
                q = re.sub(r'\s*(on|in|from|using)\s*(spotify|youtube).*$', '', q).strip()
                _SKIP = {"music", "song", "songs", "or", "and", "it", ""}
                if q and q not in _SKIP:
                    query = q
                break
        return ("spotify", {"action": "play", "query": query})

    # YouTube: search and play (skip UI actions like "click X in youtube")
    if not is_ui_action and re.search(r"\byoutube\b", lower):
        query = ""
        for pat in [r"(?:search|play)\s+(.+?)\s+(?:on|in)\s+youtube",
                    r"search\s+youtube\s+(?:for\s+)?(.+)",
                    r"play\s+(.+?)\s+on\s+youtube"]:
            m = re.search(pat, lower)
            if m:
                query = m.group(1).strip()
                break
        if query:
            return ("youtube", {"action": "play", "query": query})

    return None


# Module-level quick_chat function — set by brain.py before dispatch
_quick_chat_fn = None


def execute_api(service, params):
    """Execute via direct API handler. Returns result or None."""
    handler = _API_HANDLERS.get(service)
    if handler:
        try:
            return handler(**params)
        except Exception as e:
            logger.warning(f"API handler '{service}' failed: {e}")
            return None
    return None


@register_api("spotify")
def _spotify_api(action="play", query=""):
    """Play music via Spotify: URI protocol → UIA click → verify playback."""
    from platform_impl.windows.media import play_music
    # Expand vague queries (e.g. "good song" → "Shape of You Ed Sheeran")
    effective_query = query or ""
    return play_music(
        action="play_query" if effective_query else "play",
        query=effective_query or None,
        app="spotify",
        last_user_input=f"play {effective_query}" if effective_query else "play music",
        quick_chat_fn=_quick_chat_fn,
    )


@register_api("youtube")
def _youtube_api(action="play", query=""):
    """Search and play video on YouTube via CDP browser automation."""
    if not query:
        return None
    from platform_impl.windows.media import play_music
    return play_music(
        action="play_query",
        query=query,
        app="youtube",
        last_user_input=f"play {query} on youtube",
        quick_chat_fn=_quick_chat_fn,
    )


# ===================================================================
# UIA Strategy: Windows UI Automation
# ===================================================================

_UIA_KEYWORDS = frozenset([
    "click", "press", "button", "toggle", "switch", "checkbox",
    "select", "dropdown", "menu", "tab", "slider", "scroll",
    "type in", "fill in", "text field", "input", "form",
])


def can_use_uia(text, context=None):
    """Check if UIA is applicable."""
    lower = text.lower()
    return any(kw in lower for kw in _UIA_KEYWORDS)


def parse_uia_step(text):
    """Parse natural language step into UIA action + target + window.

    Returns: (action, target, window_name) or (None, None, None)
    """
    lower = text.lower().strip()

    # "click [the] X [button/link/menu] [in Y]"
    m = re.search(r"click\s+(?:the\s+|on\s+)?(.+?)(?:\s+(?:button|link|menu|item|checkbox|toggle|switch))?(?:\s+in\s+(.+))?$", lower)
    if m:
        return ("click", m.group(1).strip(), (m.group(2) or "").strip() or None)

    # "type X in Y" / "fill X with Y"
    m = re.search(r"(?:type|fill|enter|input)\s+(.+?)\s+(?:in|into|on)\s+(.+)", lower)
    if m:
        return ("fill", {"field": m.group(2).strip(), "text": m.group(1).strip()}, None)

    # "toggle/switch X" / "turn on/off X"
    m = re.search(r"(?:toggle|switch|turn\s+(?:on|off))\s+(?:the\s+)?(.+?)(?:\s+in\s+(.+))?$", lower)
    if m:
        return ("click", m.group(1).strip(), (m.group(2) or "").strip() or None)

    # "select X" / "press X button"
    m = re.search(r"(?:select|press|tap|check|uncheck)\s+(?:the\s+)?(.+?)(?:\s+(?:button|checkbox|option))?$", lower)
    if m:
        return ("click", m.group(1).strip(), None)

    return (None, None, None)


def execute_uia(action, target, window_name=None):
    """Execute via UI Automation with tiered resolution fallback.

    Tries: resolve_target() (UIA → role-based → vision) → raw click_control
    """
    # Try tiered resolver first for clicks (highest confidence)
    if action == "click" and isinstance(target, str):
        try:
            from automation.resolve import resolve_target
            resolved = resolve_target(target, window_name)
            if resolved and getattr(resolved, "found", False):
                logger.info(f"UIA resolved '{target}' via {resolved.source} "
                            f"(confidence={getattr(resolved, 'confidence', '?')})")
                # Use resolved action (invoke/click/focus) for best result
                if getattr(resolved, "action", "") == "invoke":
                    from automation.ui_control import click_control
                    return click_control(name=target, window=window_name) or "Invoked"
                elif resolved.x is not None and resolved.y is not None:
                    import pyautogui
                    pyautogui.click(resolved.x, resolved.y)
                    return f"Clicked '{target}' at ({resolved.x}, {resolved.y})"
        except (ImportError, Exception) as e:
            logger.debug(f"Tiered resolve failed, falling back to raw UIA: {e}")

    try:
        from automation.ui_control import (
            click_control, set_control_text, find_control, list_controls,
        )
        if action == "click":
            result = click_control(name=target, window=window_name)
            return result if result else None
        elif action == "fill":
            field = target.get("field", "") if isinstance(target, dict) else ""
            text = target.get("text", "") if isinstance(target, dict) else str(target)
            result = set_control_text(name=field, text=text, window=window_name)
            return result if result else None
        elif action == "list":
            controls = list_controls(window=window_name, max_count=20)
            if controls:
                lines = [f"  {c.get('name', '?')} ({c.get('type', '?')}) @ ({c.get('x',0)},{c.get('y',0)})"
                         for c in controls[:15]]
                return "Controls found:\n" + "\n".join(lines)
            return None
        elif action == "find":
            ctrl = find_control(name=target, window=window_name)
            if ctrl:
                return f"Found '{ctrl.get('name','')}' ({ctrl.get('type','')}) at ({ctrl.get('x',0)}, {ctrl.get('y',0)})"
            return None
    except ImportError:
        logger.debug("UI Automation not available (pywinauto not installed)")
    except Exception as e:
        logger.debug(f"UIA failed: {e}")
    return None


# ===================================================================
# CDP Strategy: Chrome DevTools Protocol for browser
# ===================================================================

_CDP_KEYWORDS = frozenset([
    "browser", "webpage", "website", "web page", "url", "navigate",
    "google", "search online", "open link", "http", "www",
    ".com", ".org", ".net", "chrome", "edge", "firefox",
])


def can_use_cdp(text, context=None):
    """Check if CDP is applicable (browser tasks)."""
    lower = text.lower()
    if any(kw in lower for kw in _CDP_KEYWORDS):
        return True
    # Also check known website names
    m = re.search(r"(?:go to|navigate to|open|visit)\s+(.+?)$", lower)
    if m and m.group(1).strip().rstrip(".") in _KNOWN_WEBSITES:
        return True
    return False


def _match_website_navigation(text):
    """Detect 'open reddit', 'go to gmail', etc. and return CDP navigate data.

    Returns: {"action": "navigate", "params": {"url": ...}} or None.
    """
    lower = text.lower().strip()
    # Remove politeness suffixes
    lower = re.sub(r"\s+(?:for me|please|now|right now|quickly|real quick)$", "", lower)
    m = re.search(r"(?:go to|navigate to|open|visit|show me|take me to|let'?s?\s+go to)\s+(.+?)$", lower)
    if not m:
        return None
    target = m.group(1).strip().rstrip(".")
    # Check known website names
    if target in _KNOWN_WEBSITES:
        return {"action": "navigate", "params": {"url": _KNOWN_WEBSITES[target]}}
    # Check bare domains (reddit.com, github.com/search)
    if re.match(r"\S+\.(?:com|org|net|io|dev|ai|co|edu|gov)", target):
        url = target if target.startswith("http") else "https://" + target
        return {"action": "navigate", "params": {"url": url}}
    return None


# Known website names → URL mapping (used by CDP parser for "open reddit", "go to gmail")
_KNOWN_WEBSITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "google maps": "https://maps.google.com",
    "google drive": "https://drive.google.com", "google docs": "https://docs.google.com",
    "netflix": "https://www.netflix.com", "twitter": "https://twitter.com",
    "x": "https://twitter.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "reddit": "https://www.reddit.com",
    "github": "https://github.com", "linkedin": "https://www.linkedin.com",
    "twitch": "https://www.twitch.tv", "amazon": "https://www.amazon.com",
    "wikipedia": "https://www.wikipedia.org", "chatgpt": "https://chat.openai.com",
    "whatsapp": "https://web.whatsapp.com", "tiktok": "https://www.tiktok.com",
    "pinterest": "https://www.pinterest.com", "spotify web": "https://open.spotify.com",
    "stackoverflow": "https://stackoverflow.com", "stack overflow": "https://stackoverflow.com",
    "notion": "https://www.notion.so", "figma": "https://www.figma.com",
    "canva": "https://www.canva.com", "zoom": "https://zoom.us",
    "dropbox": "https://www.dropbox.com", "ebay": "https://www.ebay.com",
    "hacker news": "https://news.ycombinator.com", "bing": "https://www.bing.com",
    "prime video": "https://www.primevideo.com", "disney plus": "https://www.disneyplus.com",
    "hulu": "https://www.hulu.com", "soundcloud": "https://soundcloud.com",
}


def parse_cdp_step(text):
    """Parse natural language step into CDP action + params.

    Returns: (action, params_dict) or (None, None)
    """
    lower = text.lower().strip()

    # "go to / navigate to / open / visit / take me to URL"
    m = re.search(r"(?:go to|navigate to|open|visit|take me to|let'?s?\s+go to)\s+(https?://\S+|www\.\S+|\S+\.(?:com|org|net|io|dev|ai|co|edu|gov)\S*)", lower)
    if m:
        url = m.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return ("navigate", {"url": url})

    # "open/go to/visit <known website name>" — e.g. "open reddit", "go to gmail"
    m = re.search(r"(?:go to|navigate to|open|visit|take me to|let'?s?\s+go to)\s+(.+?)$", lower)
    if m:
        site_name = m.group(1).strip().rstrip(".")
        if site_name in _KNOWN_WEBSITES:
            return ("navigate", {"url": _KNOWN_WEBSITES[site_name]})

    # "click [on] X" in browser
    m = re.search(r"click\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+(?:link|button|element))?$", lower)
    if m:
        return ("click", {"text": m.group(1).strip()})

    # "type X in Y" / "fill Y with X"
    m = re.search(r"(?:type|fill|enter)\s+(.+?)\s+(?:in|into)\s+(.+)", lower)
    if m:
        return ("fill", {"text": m.group(1).strip(), "field": m.group(2).strip()})

    # "read [the] page" / "get page content"
    if re.search(r"\bread\s+(?:the\s+)?(?:page|content|text)", lower):
        return ("read", {})

    # "search for X" on a web page
    m = re.search(r"search\s+(?:for\s+)?(.+?)(?:\s+on\s+(?:the\s+)?page)?$", lower)
    if m:
        return ("fill", {"text": m.group(1).strip(), "selector": "input[type='search'], input[name='q'], input[name='search_query']"})

    return (None, None)


def execute_cdp(action, params):
    """Execute via Chrome DevTools Protocol.

    Fast-fails if CDP is not already available (no 60s Chrome launch wait).
    For navigation, falls back to webbrowser.open if CDP is unavailable.
    """
    try:
        from automation.browser_driver import (
            browser_navigate, browser_click, browser_fill,
            browser_read, browser_get_url, browser_snapshot,
            is_cdp_available,
        )
        if not is_cdp_available():
            # Quick probe: try to connect in 3 seconds, don't launch Chrome
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(("127.0.0.1", 9222))
                sock.close()
                if result != 0:
                    # CDP port not open — for navigation, use webbrowser directly
                    if action == "navigate":
                        url = params.get("url", "")
                        if url:
                            import webbrowser
                            webbrowser.open(url)
                            domain = url.split("//")[-1].split("/")[0].replace("www.", "")
                            return f"Opened {domain} in your browser."
                    return None  # Non-navigate actions need CDP
            except Exception:
                if action == "navigate":
                    url = params.get("url", "")
                    if url:
                        import webbrowser
                        webbrowser.open(url)
                        domain = url.split("//")[-1].split("/")[0].replace("www.", "")
                        return f"Opened {domain} in your browser."
                return None

        if action == "navigate":
            return browser_navigate(params.get("url", ""))
        elif action == "click":
            return browser_click(
                text=params.get("text"), selector=params.get("selector"))
        elif action == "fill":
            return browser_fill(
                text=params.get("text"),
                field_name=params.get("field"),
                selector=params.get("selector"))
        elif action == "read":
            return browser_read(selector=params.get("selector"))
        elif action == "snapshot":
            snap = browser_snapshot()
            return json.dumps(snap, default=str)[:1000] if snap else None
        elif action == "url":
            return browser_get_url()
        elif action in ("back", "forward", "refresh"):
            from automation.browser_driver import browser_back, browser_forward
            if action == "back":
                return browser_back()
            elif action == "forward":
                return browser_forward()
            elif action == "refresh":
                # CDP Runtime.evaluate for location.reload()
                try:
                    from automation.cdp_session import get_cdp_session
                    session = get_cdp_session()
                    session.run_js("location.reload()")
                    return "Page refreshed."
                except Exception:
                    import pyautogui
                    pyautogui.hotkey("f5")
                    return "Page refreshed (keyboard)."
    except ImportError:
        logger.debug("Browser driver not available")
    except Exception as e:
        logger.debug(f"CDP failed: {e}")
    return None


# ===================================================================
# COM Strategy: Windows COM automation for Office/system apps
# ===================================================================

def can_use_com(text):
    """Check if COM automation is applicable."""
    lower = text.lower()
    com_keywords = ["excel", "word", "outlook", "powerpoint",
                    "spreadsheet", "document", "presentation"]
    return any(kw in lower for kw in com_keywords)


def execute_com(app, action, params=None):
    """Execute via Windows COM automation."""
    try:
        import win32com.client
    except ImportError:
        return None

    try:
        if app == "excel":
            xl = win32com.client.Dispatch("Excel.Application")
            xl.Visible = True
            if action == "create":
                wb = xl.Workbooks.Add()
                return f"Created new Excel workbook"
            elif action == "open":
                path = params.get("path", "") if params else ""
                if path and os.path.exists(path):
                    xl.Workbooks.Open(path)
                    return f"Opened {os.path.basename(path)}"
            return "Excel ready"

        elif app == "word":
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = True
            if action == "create":
                word.Documents.Add()
                return "Created new Word document"
            elif action == "open":
                path = params.get("path", "") if params else ""
                if path and os.path.exists(path):
                    word.Documents.Open(path)
                    return f"Opened {os.path.basename(path)}"
            return "Word ready"

        elif app == "outlook":
            ol = win32com.client.Dispatch("Outlook.Application")
            if action == "read":
                ns = ol.GetNamespace("MAPI")
                inbox = ns.GetDefaultFolder(6)  # olFolderInbox
                messages = inbox.Items
                messages.Sort("[ReceivedTime]", True)
                result_lines = []
                for i, msg in enumerate(messages):
                    if i >= 5:
                        break
                    result_lines.append(f"  {msg.Subject} — from {msg.SenderName}")
                return "Recent emails:\n" + "\n".join(result_lines) if result_lines else "No emails found"
            elif action == "send":
                mail = ol.CreateItem(0)
                mail.To = params.get("to", "") if params else ""
                mail.Subject = params.get("subject", "") if params else ""
                mail.Body = params.get("body", "") if params else ""
                if mail.To:
                    mail.Display()  # Show for review, don't auto-send
                    return f"Email draft created to {mail.To}"
                return "No recipient specified"

        elif app == "powerpoint":
            ppt = win32com.client.Dispatch("PowerPoint.Application")
            ppt.Visible = True
            if action == "create":
                ppt.Presentations.Add()
                return "Created new PowerPoint presentation"
            return "PowerPoint ready"

    except Exception as e:
        logger.debug(f"COM automation failed for {app}: {e}")
    return None


# ===================================================================
# Tool matching: map text to brain tools (no LLM needed)
# ===================================================================

_DIRECT_TOOL_PATTERNS = [
    # File creation with preview — "create a calculator using html" → create_file
    (r"(?:create|make|build|write)\s+(?:a\s+)?(?:beautiful\s+)?(?:(?:and\s+)?(?:functioning|working)\s+)?(.+?)\s+(?:using|with|in)\s+(?:html|python|javascript|css)",
     lambda m: {"tool": "create_file", "args": {"path": re.sub(r'[^a-z0-9]+', '_', m.group(1).strip().lower()).strip('_') + ".html", "content": ""}}),

    # Browser tab management — must be before app management to prevent "close the tab" → close_app
    (r"(?:close|shut)\s+(?:the|this|current|that)\s+tab",
     lambda m: {"tool": "press_key", "args": {"keys": "ctrl+w"}}),
    (r"(?:new\s+tab|open\s+(?:a\s+)?(?:new\s+)?tab)",
     lambda m: {"tool": "press_key", "args": {"keys": "ctrl+t"}}),
    (r"(?:next|switch)\s+tab",
     lambda m: {"tool": "press_key", "args": {"keys": "ctrl+tab"}}),
    (r"(?:previous|prev|last)\s+tab",
     lambda m: {"tool": "press_key", "args": {"keys": "ctrl+shift+tab"}}),

    # App management — exclude browser terms like "the tab", "this tab", "all"
    (r"^(?:open|launch|start|run)\s+(.+?)(?:\s+(?:app(?:lication)?|for me|please|now|real quick))*$",
     lambda m: {"tool": "open_app", "args": {"name": re.sub(r'\s+(?:for me|please|now|real quick)$', '', m.group(1).strip())}}
     if "side by side" not in m.group(1).lower() and "split" not in m.group(1).lower()
     else None),
    (r"^(?:close|quit|exit|stop|kill)\s+(.+?)(?:\s+app)?$",
     lambda m: {"tool": "close_app", "args": {"name": m.group(1).strip()}}
     if not re.search(r'\b(the\s+tab|this\s+tab|current\s+tab|all\s+tabs?)\b', m.group(1), re.I)
     else None),
    (r"^(?:minimize)\s+(.+)$",
     lambda m: {"tool": "minimize_app", "args": {"name": m.group(1).strip()}}
     if not re.search(r'\ball\b', m.group(1), re.I)
     else None),

    # YouTube search — "search for X on youtube", "search youtube for X"
    # Routes to play_music with app=youtube for full end-to-end (navigate + click video)
    (r"(?:search|look)\s+(?:for\s+)?(.+?)\s+on\s+youtube(?:\s+and\s+play.*)?",
     lambda m: {"tool": "play_music", "args": {"action": "play_query", "query": m.group(1).strip(), "app": "youtube"}}),
    (r"search\s+youtube\s+(?:for\s+)?(.+)",
     lambda m: {"tool": "play_music", "args": {"action": "play_query", "query": m.group(1).strip(), "app": "youtube"}}),
    # "play X on youtube"
    (r"play\s+(.+?)\s+on\s+youtube",
     lambda m: {"tool": "play_music", "args": {"action": "play_query", "query": m.group(1).strip(), "app": "youtube"}}),

    # Search — only simple searches, not compound "search X and do Y"
    (r"^(?:search|google)\s+(?:for\s+)?(.+)$",
     lambda m: {"tool": "google_search", "args": {"query": m.group(1).strip()}}
     if not re.search(r'\band\s+(?:play|open|show|do|then)\b', m.group(1), re.I)
     else None),

    # Weather
    (r"(?:weather|temperature|rain)\s+(?:in|for|at)\s+(.+)",
     lambda m: {"tool": "get_weather", "args": {"city": m.group(1).strip()}}),
    (r"(?:what(?:'s| is) the )?weather",
     lambda m: {"tool": "get_weather", "args": {}}),

    # Time
    (r"(?:what(?:'s| is)?\s+(?:the\s+)?(?:current\s+)?time|tell me the time)",
     lambda m: {"tool": "get_time", "args": {}}),

    # News
    (r"(?:news|headlines?)(?:\s+(?:about|on)\s+(.+))?",
     lambda m: {"tool": "get_news", "args": {"category": m.group(1).strip() if m.group(1) else "general"}}),

    # Reminders — multiple phrasings
    (r"remind\s+me\s+(?:to\s+)?(.+?)\s+(?:at|in|on)\s+(.+)",
     lambda m: {"tool": "set_reminder", "args": {"message": m.group(1).strip(), "time": m.group(2).strip()}}),
    (r"set\s+(?:a\s+)?reminder\s+(?:for\s+)?(.+?)\s+(?:to|for)\s+(.+)",
     lambda m: {"tool": "set_reminder", "args": {"message": m.group(2).strip(), "time": m.group(1).strip()}}),
    (r"set\s+(?:a\s+)?reminder\s+(?:to\s+)(.+?)(?:\s+(?:at|in|on)\s+(.+))?$",
     lambda m: {"tool": "set_reminder", "args": {"message": m.group(1).strip(), "time": (m.group(2) or "in 1 hour").strip()}}),
    (r"(?:list|show|check|my)\s+(?:my\s+)?reminders?",
     lambda m: {"tool": "list_reminders", "args": {}}),

    # Music control — direct media key dispatch
    (r"(?:pause|stop)\s+(?:the\s+)?music",
     lambda m: {"tool": "play_music", "args": {"action": "pause"}}),
    (r"(?:resume|continue|unpause)\s+(?:the\s+)?music|play\s+(?:the\s+)?music",
     lambda m: {"tool": "play_music", "args": {"action": "play"}}),
    (r"(?:next|skip)\s+(?:the\s+)?(?:song|track|music)",
     lambda m: {"tool": "play_music", "args": {"action": "next"}}),
    (r"(?:previous|prev|back)\s+(?:the\s+)?(?:song|track|music)",
     lambda m: {"tool": "play_music", "args": {"action": "previous"}}),

    # Forecast
    (r"forecast\s*(?:for|in)?\s*(.+)?",
     lambda m: {"tool": "get_forecast", "args": {"city": (m.group(1) or "").strip()}}),

    # Toggle settings — wifi, bluetooth, dark mode, etc.
    (r"(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(.+)",
     lambda m: {"tool": "toggle_setting", "args": {"setting": m.group(2).strip(), "value": m.group(1)}}),
    (r"(?:enable|disable)\s+(?:the\s+)?(.+)",
     lambda m: {"tool": "toggle_setting", "args": {"setting": m.group(1).strip(), "value": "on" if "enable" in m.string else "off"}}),
    (r"(.+?)\s+(?:on|off)$",
     lambda m: {"tool": "toggle_setting", "args": {"setting": m.group(1).strip(), "value": "on" if m.string.rstrip().endswith("on") else "off"}}
     if m.group(1).strip().lower() in {"wifi", "bluetooth", "dark mode", "night light", "hotspot", "airplane mode", "location"}
     else None),
]


def match_direct_tool(text):
    """Match text to a brain tool. Returns {"tool": name, "args": dict} or None."""
    lower = text.lower().strip()
    # Remove common prefixes
    lower = re.sub(r"^(?:please|can you|could you|hey\s+\w+|ok\s+\w+)\s*,?\s*", "", lower)

    for pattern, tool_fn in _DIRECT_TOOL_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            result = tool_fn(m)
            if result is not None:
                return result
    return None


# ===================================================================
# Split-screen detection and execution
# ===================================================================

_SPLIT_PATTERNS = [
    r"(?:open|launch|start)\s+(.+?)\s+and\s+(.+?)\s+(?:split\s*(?:screen|view)?|side\s*by\s*side|half\s*(?:and\s*half)?|next\s*to\s*each\s*other)",
    r"split\s*(?:screen|view)?\s+(.+?)\s+and\s+(.+)",
    r"(?:put|place|snap)\s+(.+?)\s+(?:on\s+(?:the\s+)?)?(?:left|right)\s+and\s+(.+?)\s+(?:on\s+(?:the\s+)?)?(?:right|left)",
    r"(?:open|launch)\s+(.+?)\s+(?:on\s+(?:the\s+)?)?(?:left|first)\s*(?:half)?\s+(?:and)\s+(.+?)\s+(?:on\s+(?:the\s+)?)?(?:right|second)\s*(?:half)?",
]


def detect_split_screen(text):
    """Detect split-screen request. Returns (app1, app2) or None."""
    lower = text.lower().strip()
    for pattern in _SPLIT_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            app1 = m.group(1).strip().rstrip(" ,.")
            app2 = m.group(2).strip().rstrip(" ,.")
            # Clean up common words
            for noise in ["also", "then", "please", "the"]:
                app1 = re.sub(rf"\b{noise}\b", "", app1).strip()
                app2 = re.sub(rf"\b{noise}\b", "", app2).strip()
            if app1 and app2:
                return (app1, app2)
    return None


def execute_split_screen(app1, app2, action_registry=None):
    """Open two apps and snap them side by side."""
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    # Open both apps in parallel
    def open_app(name):
        try:
            if action_registry and "open_app" in action_registry:
                return action_registry["open_app"](name)
            from app_finder import launch_app
            return launch_app(name)
        except Exception as e:
            return f"Failed to open {name}: {e}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(open_app, app1)
        f2 = pool.submit(open_app, app2)
        results[app1] = f1.result()
        results[app2] = f2.result()

    # Event-driven wait: poll for both windows instead of fixed sleep
    try:
        from automation.event_waiter import wait_for_window
        wait_for_window(app1, max_wait=3, interval=0.2)
        wait_for_window(app2, max_wait=3, interval=0.2)
    except ImportError:
        time.sleep(1.5)

    # Snap windows side by side
    try:
        from automation.window_manager import arrange_windows
        arrange_result = arrange_windows([app1, app2], layout="side-by-side")
        return f"Opened {app1} and {app2} side by side. {arrange_result or ''}"
    except ImportError:
        # Fallback: use pyautogui Win+Arrow
        try:
            import pyautogui
            import pygetwindow as gw

            # Find and snap first app to left
            wins1 = [w for w in gw.getWindowsWithTitle(app1) if w.visible] or \
                     [w for w in gw.getAllWindows() if app1.lower() in w.title.lower() and w.visible]
            if wins1:
                wins1[0].activate()
                time.sleep(0.3)
                pyautogui.hotkey("win", "left")
                time.sleep(0.5)

            # Find and snap second app to right
            wins2 = [w for w in gw.getWindowsWithTitle(app2) if w.visible] or \
                     [w for w in gw.getAllWindows() if app2.lower() in w.title.lower() and w.visible]
            if wins2:
                wins2[0].activate()
                time.sleep(0.3)
                pyautogui.hotkey("win", "right")

            return f"Opened {app1} (left) and {app2} (right) in split screen."
        except Exception as e:
            return f"Opened {app1} and {app2} but couldn't arrange: {e}"
    except Exception as e:
        return f"Opened both apps but split-screen failed: {e}"


# ===================================================================
# Parallel tool execution
# ===================================================================

def detect_parallel_tasks(text):
    """Detect multiple independent tasks that can run in parallel.

    Returns list of task descriptions, or empty list if not parallelizable.
    """
    lower = text.lower().strip()

    # "open X, Y, and Z" — but NOT "open X and go to/search/navigate/play"
    # Those are sequential compound intents, not parallel.
    if not re.search(r"\band\s+(?:go\s+to|navigate|search|play|find|type|click|fill)", lower):
        m = re.match(r"^(?:open|launch|start)\s+(.+)$", lower)
        if m:
            rest = m.group(1)
            # Split by "and" / "," combinations
            parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", rest)
            if len(parts) >= 2:
                return [f"open {p.strip()}" for p in parts if p.strip()]

    # "close X, Y, and Z"
    m = re.match(r"^(?:close|quit|exit)\s+(.+)$", lower)
    if m:
        rest = m.group(1)
        parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", rest)
        if len(parts) >= 2:
            return [f"close {p.strip()}" for p in parts if p.strip()]

    # "X and Y" with verb repeated: "open chrome and open notepad"
    m = re.findall(r"(?:open|launch|start|close|minimize)\s+\S+", lower)
    if len(m) >= 2:
        return m

    return []


def execute_parallel_tools(tasks, action_registry=None):
    """Execute multiple independent tool calls in parallel.

    Args:
        tasks: list of {"tool": name, "args": dict}
        action_registry: brain action registry

    Returns: list of (tool, args, result)
    """
    if not tasks:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def run_one(task):
        tool = task.get("tool", "")
        args = task.get("args", {})
        try:
            from brain import execute_tool
            result = execute_tool(tool, args, action_registry or {})
            return (tool, args, str(result)[:200])
        except Exception as e:
            return (tool, args, f"Error: {e}")

    results = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as pool:
        futures = {pool.submit(run_one, t): t for t in tasks}
        for f in as_completed(futures):
            results.append(f.result())

    return results


# ===================================================================
# Context Gathering — observe current system state for smart routing
# ===================================================================

def gather_context():
    """Gather current system context for intelligent strategy routing.

    Returns dict with active_window, browser state, running processes, etc.
    All calls are defensive (return empty/None on failure).
    """
    ctx = {
        "active_window": None,        # {"title": "...", "process": "..."}
        "browser_running": False,
        "cdp_available": False,
        "current_url": None,
        "running_processes": set(),    # lowercased process names
        "recent_actions": [],          # last 5 (tool, args, result) from brain
    }
    # Active window
    try:
        from automation.observers.windows_observer import WindowsObserver
        obs = WindowsObserver()
        win = obs.get_active_window()
        if win:
            ctx["active_window"] = {
                "title": getattr(win, "title", "") or "",
                "process": getattr(win, "process_name", "") or "",
            }
    except Exception:
        pass
    # Browser state
    try:
        from automation.observers.browser_observer import BrowserObserver
        bobs = BrowserObserver()
        ctx["browser_running"] = bobs.is_browser_running()
        ctx["cdp_available"] = bobs.is_cdp_available()
        if ctx["cdp_available"]:
            ctx["current_url"] = bobs.get_current_url()
    except Exception:
        pass
    # Running processes (lightweight — cached by observer)
    try:
        from automation.observers.windows_observer import WindowsObserver
        obs = WindowsObserver()
        for w in obs.get_all_windows():
            p = getattr(w, "process_name", "")
            if p:
                ctx["running_processes"].add(p.lower())
    except Exception:
        pass
    # Recent brain actions
    try:
        from core.state import BrainState
        ctx["recent_actions"] = list(BrainState.recent_actions)[-5:]
    except Exception:
        pass
    return ctx


# ===================================================================
# Pronoun / Context Resolution — "close this", "do that again"
# ===================================================================

def _resolve_pronouns(text, context):
    """Resolve pronouns using active window and recent actions.

    "close this" → "close Chrome" (if Chrome is focused)
    "open it" → replay last open_app target
    "play that again" → replay last play_music action
    """
    if not context:
        return text, None
    lower = text.lower().strip()

    # "close this" / "close that" / "minimize this"
    if re.search(r"^(?:close|minimize|quit|exit|kill)\s+(?:this|that|it|the app|the window)$", lower):
        win = context.get("active_window")
        if win and win.get("process"):
            action = "close" if "close" in lower or "quit" in lower or "exit" in lower or "kill" in lower else "minimize"
            proc = win["process"].replace(".exe", "")
            return f"{action} {proc}", None

    # "do that again" / "repeat" / "again"
    if re.search(r"^(?:do that again|repeat|again|one more time|replay)$", lower):
        recent = context.get("recent_actions", [])
        if recent:
            last = recent[-1]
            if isinstance(last, (list, tuple)) and len(last) >= 2:
                return None, {"tool": last[0], "args": last[1] if isinstance(last[1], dict) else {}}

    # "go back" when browser is focused
    if re.search(r"^(?:go back|back)$", lower):
        win = context.get("active_window", {})
        proc = (win.get("process") or "").lower()
        if proc in ("chrome.exe", "msedge.exe", "firefox.exe"):
            return None, {"_strategy": STRATEGY_CDP, "action": "back", "params": {}}

    # "refresh" when browser is focused
    if re.search(r"^(?:refresh|reload)(?:\s+(?:the\s+)?page)?$", lower):
        win = context.get("active_window", {})
        proc = (win.get("process") or "").lower()
        if proc in ("chrome.exe", "msedge.exe", "firefox.exe"):
            return None, {"_strategy": STRATEGY_CDP, "action": "refresh", "params": {}}

    return text, None


# ===================================================================
# Compound Intent — "open chrome and go to reddit"
# ===================================================================

def detect_compound_intent(text):
    """Split 'do X and then Y' into ordered steps.

    Returns list of step strings, or empty list if not compound.
    Ignores 'and' inside known phrases like 'search and play'.
    """
    lower = text.lower().strip()
    # Skip if it's a known single-intent phrase
    if re.search(r"(?:search|look)\s+.+\s+and\s+(?:play|open|show)", lower):
        return []  # "search X and play it" is single intent
    if re.search(r"side\s+by\s+side|split\s+screen", lower):
        return []  # handled by split-screen detector

    # "open X and go to Y" / "launch chrome and then navigate to reddit"
    m = re.search(r"^(.+?)\s+(?:and\s+then|and|then)\s+(.+)$", lower)
    if m:
        step1 = m.group(1).strip()
        step2 = m.group(2).strip()
        # Both steps must be actionable (not just filler)
        if len(step1) > 3 and len(step2) > 3:
            return [step1, step2]
    return []


# ===================================================================
# Settings Driver Integration — ms-settings: URI fast path
# ===================================================================

_SETTINGS_PREFIX = r"(?:open|show|go to|take me to)\s+(?:me\s+)?(?:the\s+)?"
_SETTINGS_PATTERNS = {
    _SETTINGS_PREFIX + r"(?:wifi|wi-fi|network)\s*settings?": "ms-settings:network-wifi",
    _SETTINGS_PREFIX + r"bluetooth\s*settings?": "ms-settings:bluetooth",
    _SETTINGS_PREFIX + r"display\s*settings?": "ms-settings:display",
    _SETTINGS_PREFIX + r"sound\s*settings?": "ms-settings:sound",
    _SETTINGS_PREFIX + r"(?:notification|notifications)\s*settings?": "ms-settings:notifications",
    _SETTINGS_PREFIX + r"battery\s*settings?": "ms-settings:batterysaver",
    _SETTINGS_PREFIX + r"power\s*settings?": "ms-settings:powersleep",
    _SETTINGS_PREFIX + r"storage\s*settings?": "ms-settings:storagesense",
    _SETTINGS_PREFIX + r"privacy\s*settings?": "ms-settings:privacy",
    _SETTINGS_PREFIX + r"update\s*settings?": "ms-settings:windowsupdate",
    _SETTINGS_PREFIX + r"personali[sz]ation\s*settings?": "ms-settings:personalization",
    _SETTINGS_PREFIX + r"keyboard\s*settings?": "ms-settings:typing",
    _SETTINGS_PREFIX + r"mouse\s*settings?": "ms-settings:mousetouchpad",
    _SETTINGS_PREFIX + r"date\s*(?:and|&)?\s*time\s*settings?": "ms-settings:dateandtime",
    _SETTINGS_PREFIX + r"(?:default\s+)?apps?\s*settings?": "ms-settings:defaultapps",
    _SETTINGS_PREFIX + r"about\s*(?:this\s+)?(?:pc|computer)?\s*settings?": "ms-settings:about",
    # Also match "X settings" at the end
    r"(?:wifi|wi-fi|network)\s*settings?$": "ms-settings:network-wifi",
    r"bluetooth\s*settings?$": "ms-settings:bluetooth",
    r"display\s*settings?$": "ms-settings:display",
    r"sound\s*(?:and\s+audio\s+)?settings?$": "ms-settings:sound",
}


def _match_settings_uri(text):
    """Match 'open wifi settings' etc. to ms-settings: URI. Returns URI or None."""
    lower = text.lower().strip()
    for pattern, uri in _SETTINGS_PATTERNS.items():
        if re.search(pattern, lower):
            return uri
    return None


def _execute_settings_uri(uri):
    """Open a Windows Settings page directly via ms-settings: URI."""
    try:
        os.startfile(uri)
        # Extract friendly name from URI
        page = uri.split(":")[-1].replace("-", " ").title()
        return f"Opened {page} settings."
    except Exception as e:
        return f"Failed to open settings: {e}"


# ===================================================================
# Failure Tracking — remember what fails, adapt ordering
# ===================================================================

_failure_counts = {}  # {(strategy, category): failure_count}
_success_counts = {}  # {(strategy, category): success_count}


def _categorize_request(text):
    """Categorize request for failure tracking."""
    lower = text.lower()
    if re.search(r"\b(play|music|song|spotify|youtube)\b", lower):
        return "media"
    if re.search(r"\b(open|launch|start|close|quit|minimize)\b", lower):
        return "app_mgmt"
    if re.search(r"\b(wifi|bluetooth|dark.mode|setting|toggle)\b", lower):
        return "settings"
    if re.search(r"\b(click|press|button|scroll|type)\b", lower):
        return "ui_interact"
    if re.search(r"\b(navigate|go to|visit|browse|website|\.com)\b", lower):
        return "web_nav"
    if re.search(r"\b(ram|cpu|disk|process|battery|uptime)\b", lower):
        return "system_info"
    return "general"


def _record_outcome(strategy, category, success):
    """Record strategy success/failure for adaptive ordering."""
    key = (strategy, category)
    if success:
        _success_counts[key] = _success_counts.get(key, 0) + 1
    else:
        _failure_counts[key] = _failure_counts.get(key, 0) + 1


def _strategy_confidence(strategy, category):
    """Get confidence score for a strategy in a category (0.0-1.0)."""
    key = (strategy, category)
    successes = _success_counts.get(key, 0)
    failures = _failure_counts.get(key, 0)
    total = successes + failures
    if total == 0:
        return 0.5  # neutral — no data
    return successes / total


# ===================================================================
# Post-Execution Verification — confirm actions actually worked
# ===================================================================

def _verify_result(strategy, data, result_str):
    """Verify a strategy's result using postcondition checks.

    Returns: True if verified (or can't verify), False if definitely failed.
    """
    try:
        from automation.verifiers.postconditions import verify
    except ImportError:
        return True  # Can't verify — trust the result string

    conditions = []

    if strategy == STRATEGY_CDP and data.get("action") == "navigate":
        url = data.get("params", {}).get("url", "")
        if url:
            # Extract domain for flexible matching
            import urllib.parse
            domain = urllib.parse.urlparse(url).netloc.replace("www.", "")
            if domain:
                conditions.append({"type": "url_contains", "value": domain})

    elif strategy == STRATEGY_TOOL:
        tool_name = data.get("tool", "")
        if tool_name == "open_app":
            app_name = data.get("args", {}).get("name", "")
            if app_name:
                conditions.append({"type": "process_running", "value": app_name})
        elif tool_name == "close_app":
            app_name = data.get("args", {}).get("name", "")
            if app_name:
                # Verify process is NOT running (invert check)
                pass  # can't express "not running" with postconditions

    if not conditions:
        return True  # No conditions to check — trust result string

    try:
        passed, details = verify(conditions)
        if not passed:
            failed = [d for d in details if not d.get("passed")]
            logger.debug(f"Verification failed: {failed}")
        return passed
    except Exception:
        return True  # Verification error — trust result string


# ===================================================================
# Strategy Selector: smart context-aware router
# ===================================================================

class StrategySelector:
    """Routes each task step to the optimal execution method.

    Intelligence features:
    - Context-aware: checks active window, browser state, running processes
    - Pronoun resolution: "close this" → close focused app
    - Compound intents: "open chrome and go to reddit" → 2 steps
    - Settings fast-path: "open wifi settings" → ms-settings: URI
    - Failure memory: adapts strategy ordering based on past success/failure
    - Post-verification: confirms actions actually worked via postconditions
    - Tiered UIA: uses resolve_target() for confidence-aware clicks

    Usage:
        selector = StrategySelector()
        result, strategy = selector.execute_step("open reddit", context=gather_context())
    """

    def select_strategies(self, step_description, context=None):
        """Rank execution strategies for a step.

        Uses context (active window, browser state, etc.) to make smarter
        ordering decisions. Falls back to static ordering when no context.

        Returns: list of (strategy_name, data_dict) in priority order.
        """
        strategies = []
        lower = step_description.lower()
        ctx = context or {}

        # --- Pre-check: Settings URI fast-path ---
        settings_uri = _match_settings_uri(step_description)
        if settings_uri:
            strategies.append(("settings", {"uri": settings_uri}))
            # Still add TOOL as fallback
            tool_match = match_direct_tool(step_description)
            if tool_match:
                strategies.append((STRATEGY_TOOL, tool_match))
            strategies.append((STRATEGY_VISION, {"step": step_description}))
            return strategies

        # --- Context-aware pre-checks ---
        active_proc = (ctx.get("active_window", {}) or {}).get("process", "").lower()
        active_title = (ctx.get("active_window", {}) or {}).get("title", "").lower()
        browser_focused = active_proc in ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe")
        spotify_running = any("spotify" in p for p in ctx.get("running_processes", set()))
        browser_running = ctx.get("browser_running", False)
        cdp_available = ctx.get("cdp_available", False)

        # Category for adaptive ordering
        category = _categorize_request(step_description)

        # 1. CLI — system operations (fastest, ~0.5s)
        cli_cmd = match_cli_command(step_description)
        if cli_cmd:
            strategies.append((STRATEGY_CLI, {"command": cli_cmd}))

        # 2. API — direct service API calls (Spotify URI, YouTube CDP, ~1-2s)
        api_match = match_api_service(step_description)
        if api_match:
            service, params = api_match
            # Smart: if Spotify isn't running and user wants spotify, still use API
            # (it will launch Spotify) but if it IS running, API is even faster
            strategies.append((STRATEGY_API, {"service": service, "params": params}))

        # 3. Website detection — "open reddit", "go to gmail" → CDP navigate
        cdp_website = _match_website_navigation(step_description)
        if cdp_website:
            if cdp_available or browser_running:
                # Browser is ready — CDP is the best choice
                strategies.append((STRATEGY_CDP, cdp_website))
            else:
                # No browser — need to open one first, then navigate
                # Insert open_app("chrome") as a pre-step hint
                cdp_website["_needs_browser"] = True
                strategies.append((STRATEGY_CDP, cdp_website))

        # 4. TOOL — brain tools (open_app, weather, etc.)
        tool_match = match_direct_tool(step_description)
        if tool_match:
            tool_name = tool_match.get("tool", "")
            tool_args = tool_match.get("args", {})

            # Smart: skip open_app if the app is already running
            if tool_name == "open_app":
                app_name = tool_args.get("name", "").lower()
                already_open = any(app_name in p for p in ctx.get("running_processes", set()))
                if already_open:
                    # App already running — just focus it instead
                    tool_match = {"tool": "focus_window", "args": {"name": app_name}}

            strategies.append((STRATEGY_TOOL, tool_match))

        # 5. COM — Office/system COM automation
        if can_use_com(lower):
            com_app = None
            for app in ["excel", "word", "outlook", "powerpoint"]:
                if app in lower:
                    com_app = app
                    break
            if com_app:
                action = "open" if "open" in lower else "create" if "create" in lower or "new" in lower else "read"
                strategies.append(("com", {"app": com_app, "action": action}))

        # 6. UIA — desktop UI interaction via accessibility tree
        if can_use_uia(step_description, context):
            uia_action, uia_target, uia_window = parse_uia_step(step_description)
            if uia_action:
                # Smart: if an app is focused, default the window context
                if not uia_window and active_proc:
                    uia_window = active_title.split(" - ")[0].split(" — ")[0].strip() or None
                strategies.append((STRATEGY_UIA, {
                    "action": uia_action, "target": uia_target, "window": uia_window
                }))

        # 7. CDP — browser interaction via Chrome DevTools Protocol
        if can_use_cdp(step_description, context):
            cdp_action, cdp_params = parse_cdp_step(step_description)
            if cdp_action:
                strategies.append((STRATEGY_CDP, {
                    "action": cdp_action, "params": cdp_params
                }))

        # 8. Vision — always available as fallback
        strategies.append((STRATEGY_VISION, {"step": step_description}))

        # --- Adaptive reordering: demote strategies that have failed recently ---
        if len(strategies) > 2:
            strategies = self._reorder_by_confidence(strategies, category)

        return strategies

    def _reorder_by_confidence(self, strategies, category):
        """Reorder strategies based on historical success rates.

        Keeps the first strategy (usually CLI or best match) in place,
        then reorders the rest by confidence score.
        """
        if len(strategies) <= 2:
            return strategies
        first = strategies[0]
        rest = strategies[1:]
        # Sort by confidence (higher = better), keeping VISION last
        rest.sort(key=lambda s: (
            -1 if s[0] == STRATEGY_VISION else _strategy_confidence(s[0], category)
        ), reverse=True)
        return [first] + rest

    def execute_step(self, step_description, context=None, action_registry=None,
                     skip_vision=True, skip_strategies=None):
        """Execute step using the best available strategy.

        Smart features:
        - Gathers context if not provided
        - Resolves pronouns ("close this" → close focused app)
        - Handles compound intents ("open chrome and go to reddit")
        - Verifies results via postconditions
        - Records outcomes for adaptive learning
        - skip_strategies: set of strategy names to skip (already tried by caller)

        Returns: (result_string, strategy_name) or (None, None)
        """
        self._last_tried_strategies = []
        # Auto-gather context if not provided
        if context is None:
            try:
                context = gather_context()
            except Exception:
                context = {}

        # --- Pronoun resolution ---
        resolved_text, direct_action = _resolve_pronouns(step_description, context)
        if direct_action:
            if "_strategy" in direct_action:
                # Direct strategy bypass (e.g. browser back/refresh)
                strat = direct_action.pop("_strategy")
                result = self._try_strategy(strat, direct_action, action_registry)
                if result is not None:
                    return (str(result), strat)
            elif "tool" in direct_action:
                # Direct tool replay
                try:
                    from brain import execute_tool
                    result = execute_tool(
                        direct_action["tool"], direct_action["args"],
                        action_registry or {})
                    if result is not None:
                        return (str(result), "replay")
                except Exception:
                    pass
        if resolved_text is not None and resolved_text != step_description:
            step_description = resolved_text
            logger.info(f"Pronoun resolved: '{step_description}'")

        # --- Compound intent detection ---
        steps = detect_compound_intent(step_description)
        if steps:
            logger.info(f"Compound intent: {steps}")
            results = []
            for step in steps:
                r, s = self.execute_step(step, context, action_registry, skip_vision)
                if r:
                    results.append(r)
                    # Update context: after opening an app, it becomes the active window
                    try:
                        context = gather_context()
                    except Exception:
                        pass
            if results:
                return (" | ".join(results), "compound")
            return (None, None)

        # --- Main strategy execution ---
        category = _categorize_request(step_description)
        strategies = self.select_strategies(step_description, context)

        for strategy, data in strategies:
            self._last_tried_strategies.append(strategy)
            if strategy == STRATEGY_VISION and skip_vision:
                return (None, None)
            # Skip strategies already tried by caller (avoid double execution)
            if skip_strategies and strategy in skip_strategies:
                logger.debug(f"Skipping strategy '{strategy}' (already tried by caller)")
                continue

            # Settings URI fast-path
            if strategy == "settings":
                result = _execute_settings_uri(data["uri"])
                if result and "failed" not in result.lower():
                    _record_outcome("settings", category, True)
                    return (result, "settings")
                continue

            # Handle CDP that needs browser opened first
            if strategy == STRATEGY_CDP and data.get("_needs_browser"):
                try:
                    from app_finder import launch_app
                    launch_app("chrome")
                    # Event-driven wait for browser window instead of fixed sleep
                    try:
                        from automation.event_waiter import wait_for_window
                        wait_for_window("chrome", max_wait=5, interval=0.2)
                    except ImportError:
                        time.sleep(2)
                    # Remove the hint flag
                    data.pop("_needs_browser", None)
                except Exception:
                    pass

            result = self._try_strategy(strategy, data, action_registry)
            if result is not None:
                result_str = str(result)
                # Smart verification: use postconditions instead of just string matching
                verified = _verify_result(strategy, data, result_str)
                has_error = any(w in result_str.lower() for w in [
                    "error:", "failed:", "not found", "blocked", "timed out"
                ])

                if not has_error and verified:
                    logger.info(f"Strategy '{strategy}' succeeded for: {step_description[:60]}")
                    _record_outcome(strategy, category, True)
                    return (result_str, strategy)
                else:
                    logger.debug(f"Strategy '{strategy}' failed (verified={verified}): {result_str[:100]}")
                    _record_outcome(strategy, category, False)

        return (None, None)

    def _try_strategy(self, strategy, data, action_registry=None):
        """Execute a single strategy. Returns result string or None."""
        try:
            if strategy == STRATEGY_CLI:
                return execute_cli(data["command"])

            elif strategy == STRATEGY_API:
                return execute_api(data["service"], data["params"])

            elif strategy == STRATEGY_TOOL:
                tool_name = data.get("tool")
                tool_args = data.get("args", {})
                if action_registry:
                    from brain import execute_tool
                    return execute_tool(tool_name, tool_args, action_registry)
                return None

            elif strategy == "com":
                return execute_com(data["app"], data["action"], data.get("params"))

            elif strategy == STRATEGY_UIA:
                return execute_uia(
                    data.get("action", "click"),
                    data.get("target", ""),
                    data.get("window"),
                )

            elif strategy == STRATEGY_CDP:
                return execute_cdp(
                    data.get("action", "navigate"),
                    data.get("params", {}),
                )

        except Exception as e:
            logger.debug(f"Strategy '{strategy}' exception: {e}")
        return None


# Module-level singleton
_selector = StrategySelector()


def get_selector():
    """Get the module-level StrategySelector instance."""
    return _selector
