# Windows Native Bridge Plan

## Executive Summary

G currently performs all Windows operations through Python, using pyautogui, pygetwindow, pywinauto, subprocess/PowerShell, winreg, and ctypes. Many of these are fragile, slow, or limited. This plan proposes a C# Windows service ("G.Bridge") that handles native operations, with Python remaining as the AI orchestration layer. Communication is via named pipes (fastest local IPC). The migration is incremental — one subsystem at a time, with Python fallbacks preserved.

---

## 1. Architecture Recommendation

```
+----------------------------+          +----------------------------+
|     Python Core            |          |     C# G.Bridge Service    |
|                            |          |                            |
|  brain.py (LLM + tools)   |  Named   |  AppManager                |
|  orchestration/ (loop)     |  Pipes   |  WindowManager             |
|  speech.py (STT/TTS)      | <------> |  SettingsManager           |
|  llm/ (classification)    |  JSON    |  FileManager               |
|  tools/ (registry+exec)   |  RPC     |  ProcessManager            |
|  memory.py (SQLite)       |          |  CredentialManager         |
|  features/ (workflows)    |          |  SystemTrayHost            |
|                            |          |  InputSimulator            |
+----------------------------+          +----------------------------+
      AI decisions,                        OS actions,
      conversation,                        native APIs,
      tool orchestration                   real-time events
```

### What stays in Python

| Component | Why Python |
|-----------|-----------|
| LLM orchestration (brain.py) | Tight integration with Ollama/OpenAI APIs; tool calling logic |
| Speech (STT/TTS) | Whisper, Silero VAD, Piper all have Python bindings |
| Mode classification | Regex + LLM-based; pure logic |
| Tool registry + executor | Core dispatch logic; no OS interaction |
| Memory (SQLite) | sqlite3 built-in; no Windows dependency |
| Workflows | Pure orchestration logic |
| Config management | JSON + simple crypto; Python is fine |
| Web agent | HTTP requests; no Windows APIs |

### What moves to C# Bridge

| Component | Why C# |
|-----------|--------|
| App discovery (registry + Start Menu) | Direct WinRT AppModel APIs; 10x faster than winreg + COM |
| Window management | Native `EnumWindows`, `SetForegroundWindow`, `ShowWindow`; reliable z-order |
| UI Automation | Direct IUIAutomation COM interface; no pywinauto wrapper overhead |
| Settings toggles | Native WinRT `Radio` API for Bluetooth/WiFi; 5 lines vs 40 lines of PowerShell reflection |
| Process management | WMI/Toolhelp32; structured data instead of parsing `tasklist` output |
| Credential storage | Windows Credential Manager (DPAPI); OS-level protection |
| System tray | Native NotifyIcon; no PyQt6 dependency for tray-only mode |
| File operations | Same APIs, but with proper ACL checking and progress reporting |
| Input simulation | `SendInput` API directly; DPI-aware coordinate handling |

### What stays as-is (no bridge needed)

| Component | Why |
|-----------|-----|
| Media keys | Already using `ctypes.windll.user32.keybd_event` directly — works fine |
| Screenshot | `pyautogui.screenshot()` is fast enough; image stays in Python for LLM |
| Ollama management | HTTP API; no Windows APIs needed |

---

## 2. Migration Boundary

The boundary is the **tool handler layer**. Currently, tool handlers in `tools/*.py` and `brain_defs.py` call directly into Python wrappers (pygetwindow, subprocess, etc.). After migration, they call the bridge instead.

```
BEFORE:
  ToolExecutor → handler → pygetwindow.getWindowsWithTitle("Chrome")

AFTER:
  ToolExecutor → handler → bridge.call("window.find", {"title": "Chrome"})
                              ↓ (named pipe)
                            C# Bridge → EnumWindows() → response
```

**Key principle**: Tool handlers become thin RPC callers. All OS logic moves to C#. Python never touches winreg, subprocess for system commands, pygetwindow, or pywinauto directly.

### Bridge Client (Python side)

A single `bridge_client.py` module provides the RPC interface:

```python
# bridge/client.py
class BridgeClient:
    """Named pipe client for G.Bridge service."""

    def call(self, method: str, params: dict = None, timeout: float = 10.0) -> dict:
        """Call a bridge method and return the result."""
        # Sends JSON-RPC over named pipe \\.\pipe\G_Bridge
        ...

    def is_available(self) -> bool:
        """Check if bridge service is running."""
        ...

# Module-level singleton
bridge = BridgeClient()
```

### Fallback Strategy

Every bridge-calling handler preserves the current Python implementation as fallback:

```python
def _handle_open_app(arguments):
    app = arguments.get("name", "")
    try:
        from bridge.client import bridge
        if bridge.is_available():
            result = bridge.call("app.launch", {"name": app})
            return result.get("message", f"Opened {app}.")
    except Exception:
        pass
    # Fallback: existing Python implementation
    from app_finder import launch_app
    return launch_app(app)
```

This means the bridge is optional — G works without it, just slower/less reliable.

---

## 3. IPC Recommendation: Named Pipes

### Options Evaluated

| Approach | Latency | Complexity | Auth | Firewall |
|----------|---------|-----------|------|----------|
| **Named Pipes** | **<1ms** | **Low** | **Built-in (Windows ACLs)** | **No issues** |
| Local HTTP | 1-5ms | Medium | Needs token | May trigger firewall |
| gRPC | 1-2ms | High | Needs certs or tokens | May trigger firewall |
| WebSocket | 1-3ms | Medium | Needs token | May trigger firewall |
| Shared Memory | <0.1ms | Very High | Manual | N/A |
| stdin/stdout | <1ms | Low | Process-level | N/A |

### Why Named Pipes

1. **Lowest latency** for local IPC (<1ms round-trip for typical payloads)
2. **Built-in Windows security** — pipe ACLs inherit from process; no auth tokens needed
3. **No firewall issues** — named pipes don't use network ports
4. **No external dependencies** — .NET has `NamedPipeServerStream` built-in; Python has `win32pipe` or can use `open(r'\\.\pipe\NAME', 'r+b')`
5. **Simple protocol** — newline-delimited JSON over a byte stream
6. **Bidirectional** — service can push events to Python (e.g., "window focused changed")

### Pipe Configuration

```
Pipe name:    \\.\pipe\G_Bridge
Direction:    Duplex (Python reads + writes, C# reads + writes)
Buffer size:  65536 bytes
Max instances: 1 (single Python client)
Security:     Default (same user only)
```

### Protocol: JSON-RPC 2.0 (simplified)

```
→ Request (Python to C#):
{"id": 1, "method": "window.find", "params": {"title": "Chrome"}}

← Response (C# to Python):
{"id": 1, "result": {"windows": [{"hwnd": 12345, "title": "Google Chrome", "pid": 6789}]}}

← Error:
{"id": 1, "error": {"code": -1, "message": "Window not found"}}

← Event (C# to Python, unsolicited):
{"event": "window.focused", "data": {"hwnd": 12345, "title": "Chrome"}}
```

---

## 4. API Contract

### Namespace Convention

Methods are namespaced as `subsystem.action`:

| Namespace | Operations |
|-----------|-----------|
| `app.*` | discover, launch, isRunning |
| `window.*` | find, list, focus, minimize, maximize, restore, close, snap, getActive |
| `settings.*` | toggle, get, list |
| `process.*` | list, find, kill |
| `file.*` | list, find, move, copy, delete, zip, unzip, size |
| `credential.*` | get, set, delete, list |
| `input.*` | type, keyPress, keyCombo, click, moveMouse, scroll |
| `system.*` | shutdown, restart, sleep, cancelShutdown, getInfo |
| `tray.*` | show, hide, setIcon, notify, addMenuItem |

### Example Request/Response Schemas

#### app.launch

```json
// Request
{
  "id": 1,
  "method": "app.launch",
  "params": {
    "name": "Chrome",
    "waitForWindow": true,
    "timeoutMs": 5000
  }
}

// Success Response
{
  "id": 1,
  "result": {
    "success": true,
    "pid": 12456,
    "windowTitle": "New Tab - Google Chrome",
    "hwnd": 65792,
    "message": "Opened Chrome."
  }
}

// Failure Response
{
  "id": 1,
  "result": {
    "success": false,
    "message": "Chrome is not installed.",
    "suggestions": ["Google Chrome Canary", "Chromium", "Brave Browser"]
  }
}
```

#### window.find

```json
// Request
{
  "id": 2,
  "method": "window.find",
  "params": {
    "title": "Chrome",
    "matchMode": "contains"  // "exact" | "contains" | "fuzzy"
  }
}

// Response
{
  "id": 2,
  "result": {
    "windows": [
      {
        "hwnd": 65792,
        "title": "GitHub - Google Chrome",
        "className": "Chrome_WidgetWin_1",
        "pid": 12456,
        "processName": "chrome",
        "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        "state": "normal",  // "normal" | "minimized" | "maximized"
        "isActive": true
      }
    ]
  }
}
```

#### window.focus

```json
// Request
{
  "id": 3,
  "method": "window.focus",
  "params": {
    "hwnd": 65792
  }
}

// Response
{
  "id": 3,
  "result": {
    "success": true,
    "message": "Focused Google Chrome."
  }
}
```

#### settings.toggle

```json
// Request
{
  "id": 4,
  "method": "settings.toggle",
  "params": {
    "setting": "bluetooth",
    "state": "on"  // "on" | "off" | "toggle"
  }
}

// Response
{
  "id": 4,
  "result": {
    "success": true,
    "setting": "bluetooth",
    "previousState": "off",
    "currentState": "on",
    "message": "Bluetooth has been turned on."
  }
}
```

#### window.getActive

```json
// Request
{
  "id": 5,
  "method": "window.getActive",
  "params": {}
}

// Response
{
  "id": 5,
  "result": {
    "hwnd": 65792,
    "title": "GitHub - Google Chrome",
    "processName": "chrome",
    "pid": 12456,
    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
    "state": "maximized"
  }
}
```

#### process.list

```json
// Request
{
  "id": 6,
  "method": "process.list",
  "params": {
    "filter": "chrome",
    "includeMemory": true
  }
}

// Response
{
  "id": 6,
  "result": {
    "processes": [
      {
        "pid": 12456,
        "name": "chrome",
        "title": "Google Chrome",
        "memoryMB": 342.5,
        "cpuPercent": 2.1,
        "startTime": "2026-03-07T10:15:30"
      }
    ],
    "count": 1
  }
}
```

#### system.getInfo

```json
// Request
{
  "id": 7,
  "method": "system.getInfo",
  "params": {
    "categories": ["cpu", "memory", "disk", "battery"]
  }
}

// Response
{
  "id": 7,
  "result": {
    "cpu": {"name": "AMD Ryzen 7 5800X", "cores": 8, "usagePercent": 12.5},
    "memory": {"totalGB": 32, "usedGB": 18.4, "availableGB": 13.6},
    "disk": [
      {"drive": "C:", "totalGB": 512, "freeGB": 187, "label": "Windows"}
    ],
    "battery": {"percent": 85, "pluggedIn": true, "timeRemainingMin": null}
  }
}
```

---

## 5. Phased Migration Order

### Phase A: Foundation (Week 1-2)

**Build the bridge infrastructure:**
1. C# console app with named pipe server
2. JSON-RPC message parser and dispatcher
3. Python `bridge/client.py` with connection management + retry
4. Health check: `bridge.ping` → `{"pong": true, "version": "0.1.0"}`
5. Auto-start bridge from `run.py` if available

**No real operations yet** — just verify the pipe communication works end-to-end.

### Phase B: Window Management (Week 2-3) — FIRST SUBSYSTEM

**Why first:**
- High frequency (open/close/minimize are top commands)
- Current implementation is fragile (pygetwindow has DPI issues, activation failures)
- Clear API boundary (window operations are well-defined)
- Easy to test (open Chrome, verify window appears)
- Safe (read-only queries + standard window operations)

**Methods:**
- `window.list` — replace `pygetwindow.getAllTitles()`
- `window.find` — replace `pygetwindow.getWindowsWithTitle()`
- `window.focus` — replace `.activate()` (uses `SetForegroundWindow` + `AllowSetForegroundWindow`)
- `window.minimize` / `window.maximize` / `window.restore` / `window.close`
- `window.snap` — replace manual coordinate math
- `window.getActive` — replace `pygetwindow.getActiveWindow()`

**Python files affected:** `actions.py`, `computer.py`, `vision.py` (active window title)

### Phase C: App Discovery & Launch (Week 3-4)

**Why second:**
- Second most common operation
- Registry scanning is slow in Python (534ms baseline)
- C# can scan registry + AppModel in parallel, with caching
- Enables future features: app icons, install status, suggested apps

**Methods:**
- `app.discover` — replace `get_app_index()` (registry + Start Menu + Uninstall scan)
- `app.launch` — replace `launch_app()` (with window wait)
- `app.isRunning` — replace `tasklist` parsing

**Python files affected:** `app_finder.py`, `brain_defs.py`

### Phase D: Settings Toggles (Week 4-5)

**Why third:**
- Current implementation is the worst code in the codebase (40-line PowerShell WinRT reflection strings)
- C# has native WinRT access (5 lines of `Windows.Devices.Radios.Radio`)
- Registry-based settings (dark mode) are straightforward in C#
- Very limited blast radius (only `brain_defs.py::_toggle_system_setting`)

**Methods:**
- `settings.toggle` — Bluetooth, WiFi, dark mode, night light, airplane mode
- `settings.get` — query current state

### Phase E: Process & System Info (Week 5-6)

**Methods:**
- `process.list` — replace `tasklist` CLI parsing
- `process.find` — replace `tasklist /FI` parsing
- `system.getInfo` — replace `psutil` for battery/CPU/RAM/disk

### Phase F: Credential Management (Week 6-7)

**Upgrade from Fernet file-based encryption to Windows Credential Manager (DPAPI):**
- `credential.set` — store API keys in Windows Credential Manager
- `credential.get` — retrieve encrypted credentials
- `credential.delete` — remove stored credentials
- Automatic migration from `config.json` encrypted values

### Phase G: Input Simulation (Week 7-8)

**Last because it's the most complex and risky:**
- `input.type` — replace `pyautogui.typewrite()` with `SendInput`
- `input.keyPress` / `input.keyCombo` — replace `pyautogui.press/hotkey`
- `input.click` — DPI-aware click with `SendInput`
- `input.scroll` — replace `pyautogui.scroll`

### Phase H: System Tray (Optional, Week 8+)

**Replace PyQt6 system tray with native C# `NotifyIcon`:**
- Allows tray presence without PyQt6 dependency
- Context menu for quick actions
- Notification popups
- Status indicator (listening/processing/idle)

---

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Bridge crashes** | G loses all OS operations | Python fallback preserved for every operation; bridge auto-restart |
| **IPC latency** | Added ~1ms per call | Named pipes are <1ms; batch operations when possible |
| **Version mismatch** | Python expects API that bridge doesn't have | Version handshake on connect; bridge reports supported methods |
| **Startup order** | Python starts before bridge is ready | Retry loop with backoff; fallback to Python-native while waiting |
| **C# deployment** | Users must install .NET runtime | Target .NET 8 (ships with Windows 11); self-contained build option |
| **Security** | Pipe could be accessed by other processes | Default pipe ACLs restrict to same user; add nonce handshake if needed |
| **Debugging** | Two processes harder to debug | Structured logging from bridge; Python can query `bridge.logs` |
| **Feature parity** | C# bridge must match all Python behaviors | Migrate one subsystem at a time; keep Python fallback until verified |
| **User adoption** | Extra install step | Bundle bridge exe with G; auto-start from run.py |

---

## 7. Rollout Strategy

### Stage 1: Opt-in (Alpha)

- Bridge is a separate download
- `config.json` gains `"use_bridge": false` (default off)
- Python fallbacks always active
- Used only by developer for testing

### Stage 2: Opt-out (Beta)

- Bridge bundled in G releases
- `run.py` auto-starts bridge if present
- `"use_bridge": true` by default
- Python fallbacks still active
- Bridge failures logged + auto-fallback

### Stage 3: Default (Stable)

- Bridge required for full functionality
- Python fallbacks deprecated (kept for emergency)
- Bridge auto-updates independently
- Performance improvements documented

---

## 8. First Subsystem: Window Management

**Recommended starting point for implementation.**

### Why Window Management First

1. **Most tested** — open/close/minimize are daily operations
2. **Clear improvement** — `SetForegroundWindow` reliably activates windows; pygetwindow sometimes fails
3. **DPI awareness** — C# handles per-monitor DPI natively; Python coordinate math breaks on mixed-DPI setups
4. **Safe** — window operations can't damage files or data
5. **Fast to implement** — Win32 window APIs are well-documented
6. **Immediately visible** — users see the reliability improvement

### C# Implementation Sketch

```csharp
// WindowManager.cs (simplified)
public class WindowManager
{
    public WindowInfo[] FindWindows(string title, MatchMode mode)
    {
        var results = new List<WindowInfo>();
        EnumWindows((hwnd, _) =>
        {
            var windowTitle = GetWindowTitle(hwnd);
            if (Matches(windowTitle, title, mode))
            {
                results.Add(new WindowInfo
                {
                    Hwnd = hwnd,
                    Title = windowTitle,
                    Pid = GetWindowProcessId(hwnd),
                    ProcessName = GetProcessName(hwnd),
                    Rect = GetWindowRect(hwnd),
                    State = GetWindowState(hwnd),
                    IsActive = hwnd == GetForegroundWindow()
                });
            }
            return true;
        }, IntPtr.Zero);
        return results.ToArray();
    }

    public bool FocusWindow(IntPtr hwnd)
    {
        if (IsIconic(hwnd)) ShowWindow(hwnd, SW_RESTORE);
        AllowSetForegroundWindow(GetWindowProcessId(hwnd));
        return SetForegroundWindow(hwnd);
    }

    public bool SnapWindow(IntPtr hwnd, SnapPosition position)
    {
        var screen = Screen.FromHandle(hwnd);
        var rect = CalculateSnapRect(screen.WorkingArea, position);
        ShowWindow(hwnd, SW_RESTORE);
        return SetWindowPos(hwnd, IntPtr.Zero, rect.X, rect.Y,
                           rect.Width, rect.Height, SWP_NOZORDER);
    }
}
```

### Python Bridge Call (replaces pygetwindow)

```python
# In tools/action_tools.py or equivalent handler:
def _handle_minimize_app(arguments):
    title = arguments.get("name", "")
    try:
        from bridge.client import bridge
        if bridge.is_available():
            r = bridge.call("window.find", {"title": title, "matchMode": "contains"})
            windows = r.get("windows", [])
            if not windows:
                return f"Couldn't find a window called {title}."
            for w in windows:
                bridge.call("window.minimize", {"hwnd": w["hwnd"]})
            return f"Minimized {title}."
    except Exception:
        pass
    # Fallback
    from actions import minimize_window
    return minimize_window(title)
```

---

## 9. Project Structure

```
G/
  bridge/
    client.py           ← Python named pipe client + fallback logic
    __init__.py
  G.Bridge/             ← C# project (separate solution)
    Program.cs           ← Entry point, pipe server, message dispatch
    Managers/
      WindowManager.cs
      AppManager.cs
      SettingsManager.cs
      ProcessManager.cs
      CredentialManager.cs
      InputSimulator.cs
      SystemTrayHost.cs
    Protocol/
      JsonRpcMessage.cs
      MessageDispatcher.cs
    G.Bridge.csproj
```

---

## 10. Success Criteria

| Metric | Current | Target | How to Verify |
|--------|---------|--------|---------------|
| Window activation reliability | ~85% (pygetwindow) | >99% | 100 focus attempts, count failures |
| App discovery time | 534ms (Python) | <100ms (C#) | `bridge.call("app.discover")` timing |
| Settings toggle success | ~70% (PowerShell WinRT) | >95% | Toggle BT on/off 10 times |
| Process listing | 200ms (tasklist parse) | <20ms (Toolhelp32) | `bridge.call("process.list")` timing |
| IPC round-trip | N/A | <2ms | `bridge.call("bridge.ping")` timing |
| DPI-aware window snap | Broken on mixed DPI | Works correctly | Snap on 4K + 1080p dual monitor |
| Bridge startup | N/A | <500ms | Time from `run.py` launch to pipe ready |
