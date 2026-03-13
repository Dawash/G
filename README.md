# G - Personal AI Operating System

A voice-first AI operating system for Windows that listens, understands, and acts. Built from scratch with 45,000+ lines of Python.

G is your personal AI that controls your entire computer through natural voice commands. It opens apps, browses the web, plays music, manages files, automates desktop tasks, and has full conversations -- all hands-free. Powered by local AI (Ollama, qwen2.5:32b) with no cloud dependency for core features.

## Demo

```
You: "Open Chrome and go to Reddit"
G: Opens Chrome, navigates to reddit.com

You: "Play some chill music"
G: Opens Spotify, plays a relaxing playlist

You: "What's the weather like?"
G: "It's 51 degrees in your area with light drizzle. Take an umbrella!"

You: "Create a beautiful calculator using HTML, CSS, and JavaScript"
G: Generates a full calculator app and opens it in browser

You: "Introduce yourself in Nepali"
G: "Namaste! Ma G, tapaaiko vyaktigat AI sahaayak hu..."

You: "Open Notepad and Chrome side by side"
G: Launches both apps and snaps them to left/right halves

You: "How much RAM am I using?"
G: "You're using 24.3 GB out of 47.7 GB (51%)"
```

## Features

### Voice Control
- **Wake word detection** - Say "Hey G" to activate (customizable name)
- **Continuous listening** - Silero VAD + Whisper STT with GPU acceleration
- **Natural TTS** - Piper (English, neural, offline) + gTTS (Hindi, Nepali, 30+ languages)
- **Speech barge-in** - Interrupt G mid-sentence with a new command
- **Multilingual** - Auto-detects and responds in the right language

### Smart Routing (12-Layer Strategy)
Every command is intelligently routed through the fastest possible execution path:

| # | Layer | What | Example | Speed |
|---|-------|------|---------|-------|
| 1 | Cache | Replay identical recent results | Repeated query within 30s | 0ms |
| 2 | CLI | PowerShell/CMD system operations | "How much disk space?" | <1s |
| 3 | Settings | Windows ms-settings: URI fast-path | "Open display settings" | <1s |
| 4 | API | Direct service integrations | "Play a song on Spotify" | 1-2s |
| 5 | Website | Known site navigation (35+ sites) | "Open Reddit" | 1-2s |
| 6 | Tool | Brain tool calls | "What time is it?" | <1s |
| 7 | COM | Win32 COM for Office apps | "Create a Word document" | <1s |
| 8 | UIA | Windows Accessibility tree | "Click the search box" | <1s |
| 9 | CDP | Chrome DevTools / Playwright | "Go to github.com" | 1-2s |
| 10 | Compound | Chain multiple strategies | "Open Chrome and go to Reddit" | varies |
| 11 | Agent | Full desktop agent with vision | "Order a pizza from Domino's" | 5-30s |
| 12 | Vision | Screenshot + LLM analysis | "What's on my screen?" | 5-10s |

Includes context-aware routing, pronoun resolution ("close this" closes the focused app), adaptive reordering (demotes strategies that fail), parallel execution with cancellation, result caching, and postcondition verification.

### LLM Brain (48+ Tools)
The AI brain powered by Ollama (local, free) with smart model scaling -- supports 7B to 72B+ models with automatic timeout and context window adjustment:

- **App management** - Open, close, minimize, split-screen any application
- **Web browsing** - Navigate websites, search, read web pages
- **Music control** - Spotify/YouTube play, pause, skip, volume
- **File operations** - Create, move, copy, delete, zip files
- **System info** - RAM, CPU, disk, processes, network stats
- **Weather & news** - Real-time weather, forecasts, news headlines
- **Reminders & alarms** - Natural language ("remind me at 5pm")
- **Email** - Send emails via SMTP
- **Desktop automation** - Click, type, scroll, keyboard shortcuts
- **Vision** - Screenshot analysis, element detection
- **Terminal** - Run any PowerShell/CMD command
- **Code interpreter** - Safe Python sandbox for math, data processing, logic

### Multi-Agent Swarm (6 Agents)
For complex multi-step tasks, G deploys a coordinated team of specialized agents:

```
SwarmOrchestrator
  |
  +-- PlannerAgent     Tree-of-Thoughts: generates 3 approaches, LLM scores, decomposes best
  +-- ExecutorAgent    3-tier dispatch: direct tools -> strategy selector -> desktop agent
  +-- CriticAgent      Self-consistency scoring every 3 steps, stuck detection
  +-- ResearcherAgent  Web research when stuck, cached solution lookup
  +-- MemoryAgent      Skill evolution + reflexion learning on completion
  +-- DebateAgent      Multi-perspective deliberation (advocate, skeptic, pragmatist)
```

**State machine:** PLAN -> EXECUTE -> CRITIQUE -> (continue | RESEARCH | REPLAN) -> ... -> DONE -> LEARN

**Budget controls:** max 30 actions, 40 LLM calls, 300s timeout, 3 replans.

The debate agent triggers on ambiguous decisions -- spawns three viewpoints (advocate, skeptic, pragmatist) that argue, cross-examine, and a moderator picks the winning approach.

### Voyager-Style Skill Library
- FAISS vector DB for semantic skill search (with TF-IDF fallback if FAISS unavailable)
- Successful multi-step action sequences saved as reusable skills
- Skills auto-replay when similar requests come in (similarity >= 0.70)
- Stale skill pruning (unused skills cleaned after 7 days)
- Activation triggers for fast regex-based matching before vector search

### Reflexion Learning
- Failure lessons stored in vector memory for future avoidance
- Oscillation detection (A->B->A->B patterns) forces alternative approaches
- Diagnosis uses stored reflexions to avoid repeating mistakes

### Context-Aware Intelligence
- **Pronoun resolution** - "Close this" closes the focused app, "go back" navigates browser history
- **Compound commands** - "Open Chrome and go to Reddit" chains two actions
- **Failure memory** - Remembers what works and adapts strategy selection
- **Topic tracking** - Maintains context across multi-turn conversations
- **Routine detection** - "You usually open Spotify around now"

### Tree-of-Thoughts Planning
The PlannerAgent generates 3 candidate approaches for complex tasks, scores each via LLM evaluation (0-100), selects the best, and decomposes it into executable steps. Falls back to linear planning for simpler goals.

### Browser Automation (Playwright + CDP)
- **Playwright** for cross-browser automation (Chrome, Firefox, WebKit) with auto-wait, smart selectors, retry logic, and network interception
- **CDP fallback** for direct Chrome DevTools Protocol when Playwright is unavailable
- Connects to existing Chrome instances or launches new ones
- 14 browser actions: navigate, click, fill, read, screenshot, back, forward, etc.

### Code Interpreter
- Safe Python sandbox with 30-second timeout
- Restricted imports (math, statistics, json, csv, etc.) -- no network, no filesystem writes
- 256MB memory limit
- Handles math calculations, data processing, code generation + execution

### Session Persistence
- Auto-saves conversation context, topic, and metadata across restarts
- Restores last 20 messages, current topic, tool blacklist on startup
- Atomic writes with 24-hour freshness check
- Survives crashes gracefully

### WebSocket Gateway
- Remote control via `ws://localhost:8765`
- Web UI served on port 8766 (mobile-first dark theme)
- Token-based authentication
- Endpoints: think, quick_chat, tool execution, status

### Persistent Memory
- SQLite-backed long-term memory
- Learns your preferences (favorite apps, response style)
- Nickname system ("my browser" = Firefox)
- Habit tracking with proactive suggestions

## Quick Start

### Prerequisites
- **Windows 10/11**
- **Python 3.12** (recommended) or 3.14
- **Microphone + speakers**
- **Ollama** (for local AI - free, no API key needed)

### Installation

```bash
# Clone the repository
git clone https://github.com/Dawash/G.git
cd G

# Run the auto-launcher (handles everything)
python run.py
```

The launcher automatically:
1. Checks Python version
2. Installs all dependencies (including playwright, faiss-cpu, sentence-transformers)
3. Prompts for your name and AI preferences
4. Lets you choose Ollama model size (7B/14B/32B based on your RAM)
5. Downloads chosen model + Whisper STT + Piper TTS
6. Starts the assistant

Type "back" at any prompt to go to the previous step.

### Manual Installation

```bash
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Install Ollama from https://ollama.ai
ollama pull qwen2.5:32b   # or qwen2.5:7b for lower RAM

# Run
python main.py
```

### First Run Setup
On first run, G walks you through an interactive wizard:
1. **Your name** - Used for personalized greetings
2. **AI name** - What you want to call your AI (default: "G")
3. **AI provider** - Ollama (local, free), OpenAI, Anthropic, or OpenRouter
4. **Model selection** - Choose model size based on your RAM (7B/14B/32B/72B)
5. **API key** - Only needed for cloud providers (encrypted on disk)

You can type "back" at any step to go back and change your choice.

## Configuration

Settings are stored in `config.json` (auto-generated, never committed):

```json
{
  "username": "YourName",
  "ai_name": "G",
  "provider": "ollama",
  "ollama_model": "qwen2.5:32b",
  "ollama_url": "http://localhost:11434"
}
```

### Supported AI Providers

| Provider | Model | Cost | Setup |
|----------|-------|------|-------|
| **Ollama** (default) | qwen2.5:7b-72b | Free (local) | Auto-selected by RAM |
| OpenAI | gpt-4o-mini | Pay per use | API key |
| Anthropic | Claude Sonnet | Pay per use | API key |
| OpenRouter | Any model | Pay per use | API key |

Switch providers at any time by saying: "Switch to OpenAI"

## Architecture

```
run.py                          Auto-launcher (deps, Ollama, validation)
main.py                         Entry point
assistant.py                    State machine (IDLE/ACTIVE), wake word
brain.py                        LLM Brain, 48+ tools, 3-tier tool calling
brain_defs.py                   Tool definitions & safety rules
execution_strategies.py         12-layer smart routing
speech.py                       Silero VAD + Whisper + Piper/gTTS
config.py                       Configuration management
ai_providers.py                 Ollama/OpenAI/Anthropic/OpenRouter
intent.py                       Keyword fallback (offline)
actions.py                      App launch, window management
app_finder.py                   Smart app discovery (registry + fuzzy)
desktop_agent.py                Autonomous desktop automation
vision.py                       Screenshot + llava analysis
computer.py                     Mouse/keyboard control (pyautogui)
web_agent.py                    Web reading + DuckDuckGo search
memory.py                       SQLite persistent memory
cognitive.py                    6-phase cognitive engine
skills.py                       Voyager-style skill library
embeddings.py                   FAISS vector store + sentence-transformers
email_sender.py                 SMTP email
reminders.py                    NLP time parsing, recurring
weather.py                      Open-Meteo (free, no API key)
news.py                         Google News RSS
alarms.py                       Wake-up alarm system
user_choice.py                  Interactive multi-choice
self_test.py                    Runtime diagnostics (24 tests)
```

### Safety & Contracts (core/)

```
core/execution_tiers.py         4-tier autonomy (DETERMINISTIC → HUMAN_REQUIRED)
core/tool_contracts.py          Typed ABI: JSON Schema validation, side-effects, rollback
core/failure_journal.py         SQLite failure corpus with pattern clustering
core/state.py                   Shared provider state (rate limits, health)
core/session_persistence.py     Atomic session save/restore
core/control_flags.py           Runtime control flags
```

### Multi-Agent System (agents/)

```
agents/orchestrator.py          SwarmOrchestrator state machine
agents/planner.py               Tree-of-Thoughts planner (3 branches)
agents/executor.py              3-tier dispatch executor
agents/critic.py                Self-consistency scoring + stuck detection
agents/researcher.py            Web research when stuck
agents/memory_agent.py          Skill evolution + reflexion learning
agents/debate.py                Multi-perspective deliberation
agents/blackboard.py            Shared state + vector memory + checkpointing
agents/base.py                  BaseAgent class
```

### Automation & Tools

```
automation/playwright_session.py  Playwright browser session (CDP fallback)
automation/cdp_session.py         Chrome DevTools Protocol session
tools/code_interpreter.py         Safe Python sandbox
tools/browser_tools.py            Browser action tool (14 actions)
tools/builtin_tools.py            Core tool implementations
tools/registry.py                 Tool registry
tools/isolated_executor.py        Process isolation for risky tools
core/session_persistence.py       Session save/restore across restarts
gateway/ws_server.py              WebSocket server for remote control
gateway/http_server.py            HTTP server + web UI
gateway/web_ui.html               Mobile-first dark-theme dashboard
```

### Data Flow

```
Microphone -> Silero VAD -> Whisper STT -> Smart Router (12 layers)
                                              |-> Cache replay (instant)
                                              |-> Direct dispatch (CLI/Settings/API/Website/Tool)
                                              |-> Brain (LLM + tool calling)
                                              |-> Multi-Agent Swarm (complex tasks)
                                              '-> Desktop Agent (UI automation)
                                                        |
                                                  Execute + Verify
                                                        |
                                              Piper/gTTS -> Speaker
```

## Project Structure

```
G/
+-- run.py                      # Auto-launcher
+-- main.py                     # Entry point
+-- assistant.py                # State machine
+-- brain.py                    # LLM Brain (48+ tools)
+-- brain_defs.py               # Tool definitions & safety
+-- execution_strategies.py     # 12-layer smart routing
+-- speech.py                   # STT + TTS + wake word
+-- config.py                   # Configuration management
+-- ai_providers.py             # Ollama/OpenAI/Anthropic/OpenRouter
+-- intent.py                   # Keyword fallback (offline)
+-- actions.py                  # App launch, window management
+-- app_finder.py               # Smart app discovery
+-- desktop_agent.py            # Autonomous desktop automation
+-- vision.py                   # Screenshot + llava analysis
+-- computer.py                 # Mouse/keyboard control
+-- web_agent.py                # Web reading + search
+-- memory.py                   # SQLite persistent memory
+-- cognitive.py                # 6-phase cognitive engine
+-- skills.py                   # Voyager-style skill library
+-- embeddings.py               # FAISS vector store + embeddings
+-- reminders.py                # NLP time parsing, recurring
+-- weather.py                  # Open-Meteo (free, no API key)
+-- news.py                     # Google News RSS
+-- email_sender.py             # SMTP email
+-- alarms.py                   # Wake-up alarm system
+-- user_choice.py              # Interactive multi-choice
+-- self_test.py                # Runtime diagnostics
+-- requirements.txt            # Python dependencies
|
+-- agents/                     # Multi-agent swarm (6 agents + blackboard + orchestrator)
+-- automation/                 # Playwright, CDP, observers
+-- core/                       # Execution tiers, tool contracts, failure journal, state
+-- dashboard/                  # PyQt6 GUI dashboard
+-- features/                   # Memory & workflow subsystems
+-- gateway/                    # WebSocket + HTTP remote control
+-- llm/                        # LLM adapters, prompt builder
+-- models/                     # Whisper + Piper models (auto-downloaded)
+-- orchestration/              # Assistant loop, routing, recovery
+-- platform_impl/              # Windows-specific implementations
+-- sounds/                     # Alarm audio files
'-- tools/                      # Tool registry, executors, code interpreter
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **STT** | faster-whisper (GPU, CUDA) |
| **VAD** | Silero VAD |
| **TTS (English)** | Piper (neural, offline) |
| **TTS (Other)** | gTTS (30+ languages) |
| **LLM** | Ollama (qwen2.5:32b default, 7B-72B supported) |
| **Vision** | llava (screen understanding) |
| **Browser** | Playwright (primary) + Chrome DevTools Protocol (fallback) |
| **UI Automation** | pywinauto + pyautogui |
| **Vector DB** | FAISS + sentence-transformers (TF-IDF fallback) |
| **Memory** | SQLite |
| **Dashboard** | PyQt6 + QWebEngine |
| **Remote** | WebSocket + HTTP |
| **Code Execution** | Sandboxed Python subprocess |

## Tutorials

### Tutorial 1: Basic Voice Commands
```
# Start the assistant
python run.py

# Wait for "What can I do for you?"
# Then say any of these:

"What time is it?"              -> Gets current time
"What's the weather?"           -> Current weather + forecast
"Open calculator"               -> Launches Windows Calculator
"Close calculator"              -> Closes it
"How much RAM am I using?"      -> Shows memory stats
"Open display settings"         -> Opens Windows Settings
"Tell me a joke"                -> LLM generates a joke
```

### Tutorial 2: Web Navigation
```
"Open Reddit"                   -> Opens reddit.com in browser
"Go to GitHub"                  -> Navigates to github.com
"Visit Gmail"                   -> Opens mail.google.com
"Search for Python tutorials"   -> Google search
```

### Tutorial 3: Music Control
```
"Play some jazz"                -> Plays music via media keys
"Play Shape of You on Spotify"  -> Opens Spotify, searches, plays (agent mode)
"Search for lo-fi on YouTube"   -> Opens YouTube, searches, plays first video
"Pause the music"               -> Pauses playback
"Next song"                     -> Skips to next track
```

### Tutorial 4: File Creation
```
"Create a calculator using HTML, CSS, and JavaScript"
-> Generates a complete, styled, functional calculator
   and saves it as an HTML file

"Create a to-do list app"
-> Generates a full web app with add/delete/complete features
```

### Tutorial 5: System Management
```
"List running processes"             -> Shows all active processes
"How much disk space is free?"       -> Drive usage stats
"What's my IP address?"              -> Network information
"Open notepad and chrome side by side" -> Split-screen layout
"Minimize all apps"                  -> Minimizes everything
```

### Tutorial 6: Reminders
```
"Remind me to take a break in 30 minutes"
"Remind me to call mom at 5pm"
"Set a reminder for every Monday at 9am to check email"
"What are my reminders?"
"Clear all reminders"
```

### Tutorial 7: Compound Commands
```
"Open Chrome and go to Reddit"
-> Chains: launch Chrome -> navigate to reddit.com

"Open Notepad and Chrome side by side"
-> Chains: launch both -> snap to left/right halves
```

### Tutorial 8: Agent Mode (Complex Tasks)
```
"Order me a pizza from Domino's"
-> G launches browser, navigates to Domino's, asks you what to order

"Log into Gmail and send an email to John"
-> G opens Gmail, asks for your credentials, composes email

"Book a flight to Tokyo"
-> G searches for flights, presents options, asks you to choose
```

Agent mode uses vision (screenshots) to understand the screen and makes decisions about what to click, type, or do next. It asks for user input when needed (login, choices, etc.) instead of guessing.

### Tutorial 9: Multilingual
```
"Introduce yourself in Nepali"
-> Responds in Nepali with proper Devanagari script + gTTS voice

"Say hello in French"
-> Responds in French with natural pronunciation
```

## Requirements
- Windows 10/11
- Python 3.12+ (also works with 3.14)
- Microphone + speakers
- ~8GB RAM minimum (16GB+ recommended for 14B/32B models)
- GPU recommended (CUDA) for Whisper acceleration
- Internet for weather, news, web features (core works offline)

### Key Python Dependencies
- `playwright` - Cross-browser automation
- `faiss-cpu` - Vector similarity search
- `sentence-transformers` - Text embeddings (all-MiniLM-L6-v2)
- `faster-whisper` - GPU-accelerated speech-to-text
- `piper-tts` - Neural text-to-speech
- `pywinauto` - Windows UI automation
- `pyautogui` - Mouse/keyboard control
- `PyQt6` - GUI dashboard

See `requirements.txt` for the full list.

## Troubleshooting

### Common Issues

**"ModuleNotFoundError: No module named 'faster_whisper'"**
```bash
pip install faster-whisper
# Or reinstall everything:
pip install -r requirements.txt
```

**"Ollama not running" / Connection refused**
```bash
# Start the Ollama server
ollama serve
# In another terminal, verify:
curl http://localhost:11434
```

**PyAudio fails to install**
```bash
# Windows - use pre-built binary
pip install pyaudio --only-binary=:all:
# Or install Visual C++ Build Tools first
```

**"CUDA not available" (slow STT)**
- Install NVIDIA CUDA Toolkit 11.8+
- Whisper falls back to CPU (slower but works)

**Microphone not working**
- Check Windows Settings > Privacy > Microphone access
- Run `python -c "import pyaudio; print(pyaudio.PyAudio().get_default_input_device_info())"` to verify
- Try: `python run.py --selftest`

**Ollama model download stalls**
```bash
# Cancel and retry
ollama pull qwen2.5:32b
# If behind proxy, set HTTP_PROXY/HTTPS_PROXY
```

**"piper-tts" install fails**
- Piper requires specific Python version compatibility
- Fallback TTS (pyttsx3) works without it - G auto-detects

**Agent mode times out**
- Large models (32B+) need more time - increase `ollama_timeout` in config.json
- Try a smaller model: `ollama pull qwen2.5:7b`

**Vision features not working**
```bash
ollama pull llava
# Verify: ollama list (should show llava)
```

**Playwright not working**
```bash
pip install playwright
playwright install chromium
# Falls back to CDP if Playwright unavailable
```

### Run Diagnostics

```bash
# Full self-test (checks all 17 subsystems)
python run.py --selftest

# Or from Python:
from self_test import run_self_test
print(run_self_test())
```

### Update G

```bash
python run.py --update
# Pulls latest code, updates deps, refreshes Ollama model
```

## License

MIT License - see [LICENSE](LICENSE)

## Credits

Created by **Dawa Sangay Sherpa** as a personal AI operating system project.

Built with: Ollama, faster-whisper, Silero VAD, Piper TTS, gTTS, Playwright, FAISS, sentence-transformers, PyQt6, pyautogui, pywinauto, and many other open-source libraries.
