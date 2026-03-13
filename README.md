# G - Personal AI Operating System

A voice-first AI operating system for Windows that listens, understands, and acts. Built from scratch with 45,000+ lines of Python.

G is your personal AI that controls your entire computer through natural voice commands. It opens apps, browses the web, plays music, manages files, automates desktop tasks, and has full conversations — all hands-free. Powered by local AI (Ollama) with no cloud dependency for core features.

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
G: "नमस्कार! म G, तपाईंको व्यक्तिगत AI सहायक हुँ..."

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

### Smart Routing (8-Layer Strategy)
Every command is intelligently routed through the fastest possible path:

| Layer | What | Example | Speed |
|-------|------|---------|-------|
| CLI | Terminal commands | "How much disk space?" | <1s |
| API | Service integrations | "Play a song on Spotify" | 2-3s |
| Website | Browser navigation | "Open Reddit" | 1-2s |
| Tool | Direct tool calls | "What time is it?" | <1s |
| Settings | Windows settings | "Open display settings" | <1s |
| UIA | UI Automation | "Click the search box" | 2-5s |
| CDP | Chrome DevTools | "Go to github.com" | 2-3s |
| Vision | Screen analysis | "What's on my screen?" | 3-5s |

### LLM Brain (48 Tools)
The AI brain powered by Ollama (local, free) with smart model scaling — supports 7B to 70B+ models with automatic timeout and context window adjustment:

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

### Context-Aware Intelligence
- **Pronoun resolution** - "Close this" closes the focused app, "go back" navigates browser history
- **Compound commands** - "Open Chrome and go to Reddit" chains two actions
- **Failure memory** - Remembers what works and adapts strategy selection
- **Topic tracking** - Maintains context across multi-turn conversations
- **Routine detection** - "You usually open Spotify around now"

### Desktop Agent (Agentic Mode)
For complex multi-step tasks, G plans and executes autonomously:

**Simple agent tasks** use the legacy 3-phase loop:
```
Plan → Observe (screenshot) → Think → Act → Verify → Diagnose if failed
```

**Complex multi-step tasks** (updated Mar 2026) use the **5-agent swarm**:
```
PlannerAgent (Tree-of-Thoughts) → ExecutorAgent → CriticAgent → ResearcherAgent → MemoryAgent
```

The swarm generates 3 candidate approaches, scores them via LLM, decomposes the best into executable steps, and runs them with periodic quality checks. If stuck, the researcher searches the web for solutions. On completion, the memory agent saves successful sequences as reusable skills.

- **UI-interactive tasks** auto-route to agent mode (Spotify, YouTube, ordering, login flows)
- **Multi-agent swarm** for complex tasks — 5 specialized agents with shared blackboard
- **Budget controls** — max 30 actions, 40 LLM calls, 300s timeout, 3 replans
- Silent execution — thinks in console, speaks only the result
- Self-healing with web research when stuck
- Voyager-style skill library saves successful action sequences
- Reflexion learning — failure lessons stored for future avoidance
- Code interpreter — safe Python sandbox for math/data tasks
- Configurable per-tool timeouts via `config.json`

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
2. Installs all dependencies
3. Prompts for your name and AI preferences
4. Lets you choose Ollama model size (7B/14B/32B based on your RAM)
5. Downloads chosen model + Whisper STT + Piper TTS
6. Starts the assistant

Type "back" at any prompt to go to the previous step.

### Manual Installation

```bash
pip install -r requirements.txt

# Install Ollama from https://ollama.ai
ollama pull qwen2.5:7b

# Run
python main.py
```

### First Run Setup
On first run, G walks you through an interactive wizard:
1. **Your name** — Used for personalized greetings
2. **AI name** — What you want to call your AI (default: "G")
3. **AI provider** — Ollama (local, free), OpenAI, Anthropic, or OpenRouter
4. **Model selection** — Choose model size based on your RAM (7B/14B/32B/72B)
5. **API key** — Only needed for cloud providers (encrypted on disk)

You can type "back" at any step to go back and change your choice.

## Configuration

Settings are stored in `config.json` (auto-generated, never committed):

```json
{
  "username": "YourName",
  "ai_name": "G",
  "provider": "ollama",
  "ollama_model": "qwen2.5:7b",
  "ollama_url": "http://localhost:11434"
}
```

### Supported AI Providers

| Provider | Model | Cost | Setup |
|----------|-------|------|-------|
| **Ollama** (default) | qwen2.5:7b-32b | Free (local) | Auto-selected by RAM |
| OpenAI | gpt-4o-mini | Pay per use | API key |
| Anthropic | Claude Sonnet | Pay per use | API key |
| OpenRouter | Any model | Pay per use | API key |

Switch providers at any time by saying: "Switch to OpenAI"

## Architecture

```
run.py              → Auto-launcher (deps, Ollama, validation)
main.py             → Entry point
assistant.py        → State machine (IDLE/ACTIVE), wake word
brain.py            → LLM Brain, 48 tools, 3-tier tool calling
execution_strategies.py → 8-layer smart routing
speech.py           → Silero VAD + Whisper + Piper/gTTS
desktop_agent.py    → Autonomous desktop automation
memory.py           → SQLite persistent memory
skills.py           → Voyager-style skill library
```

### Data Flow
```
Microphone → Silero VAD → Whisper STT → Smart Router
                                           ├→ Direct dispatch (CLI/API/Website/Tool)
                                           ├→ Brain (LLM + tool calling)
                                           └→ Desktop Agent (complex tasks)
                                                     ↓
                                              Execute + Verify
                                                     ↓
                                           Piper/gTTS → Speaker
```

## Tutorials

### Tutorial 1: Basic Voice Commands
```
# Start the assistant
python run.py

# Wait for "What can I do for you?"
# Then say any of these:

"What time is it?"              → Gets current time
"What's the weather?"           → Current weather + forecast
"Open calculator"               → Launches Windows Calculator
"Close calculator"              → Closes it
"How much RAM am I using?"      → Shows memory stats
"Open display settings"         → Opens Windows Settings
"Tell me a joke"                → LLM generates a joke
```

### Tutorial 2: Web Navigation
```
"Open Reddit"                   → Opens reddit.com in browser
"Go to GitHub"                  → Navigates to github.com
"Visit Gmail"                   → Opens mail.google.com
"Search for Python tutorials"   → Google search
```

### Tutorial 3: Music Control
```
"Play some jazz"                → Plays music via media keys
"Play Shape of You on Spotify"  → Opens Spotify, searches, clicks result (agent mode)
"Search for lo-fi on YouTube"   → Opens YouTube, searches, plays first video
"Pause the music"               → Pauses playback
"Next song"                     → Skips to next track
```

### Tutorial 4: File Creation
```
"Create a calculator using HTML, CSS, and JavaScript"
→ G generates a complete, styled, functional calculator
  and saves it as an HTML file

"Create a to-do list app"
→ Generates a full web app with add/delete/complete features
```

### Tutorial 5: System Management
```
"List running processes"        → Shows all active processes
"How much disk space is free?"  → Drive usage stats
"What's my IP address?"         → Network information
"Open notepad and chrome side by side" → Split-screen layout
"Minimize all apps"             → Minimizes everything
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
→ Chains: launch Chrome → navigate to reddit.com

"Open Notepad and Chrome side by side"
→ Chains: launch both → snap to left/right halves
```

### Tutorial 8: Agent Mode (Complex Tasks)
```
"Order me a pizza from Domino's"
→ G launches browser, navigates to Domino's, asks you what to order

"Log into Gmail and send an email to John"
→ G opens Gmail, asks for your credentials, composes email

"Book a flight to Tokyo"
→ G searches for flights, presents options, asks you to choose
```

Agent mode uses vision (screenshots) to understand the screen and makes decisions about what to click, type, or do next. It asks for user input when needed (login, choices, etc.) instead of guessing.

### Tutorial 9: Multilingual
```
"Introduce yourself in Nepali"
→ Responds in Nepali with proper Devanagari script + gTTS voice

"Say hello in French"
→ Responds in French with natural pronunciation
```

## Project Structure

```
G/
├── run.py                  # Auto-launcher
├── main.py                 # Entry point
├── assistant.py            # State machine shim
├── brain.py                # LLM Brain (48 tools)
├── brain_defs.py           # Tool definitions & safety
├── execution_strategies.py # 8-layer smart routing
├── speech.py               # STT + TTS + wake word
├── config.py               # Configuration management
├── ai_providers.py         # Ollama/OpenAI/Anthropic/OpenRouter
├── intent.py               # Keyword fallback (offline)
├── actions.py              # App launch, window management
├── app_finder.py           # Smart app discovery
├── desktop_agent.py        # Autonomous desktop automation
├── vision.py               # Screenshot + llava analysis
├── computer.py             # Mouse/keyboard control
├── web_agent.py            # Web reading + search
├── memory.py               # SQLite persistent memory
├── cognitive.py            # 6-phase cognitive engine
├── skills.py               # Voyager-style skill library
├── reminders.py            # NLP time parsing, recurring
├── weather.py              # Open-Meteo (free, no API key)
├── news.py                 # Google News RSS
├── email_sender.py         # SMTP email
├── alarms.py               # Wake-up alarm system
├── self_test.py            # Runtime diagnostics
├── requirements.txt        # Python dependencies
│
├── agents/                 # Multi-agent swarm (5 agents + blackboard + orchestrator)
├── automation/             # Browser drivers, CDP, observers
├── core/                   # Control flags, metrics, state
├── dashboard/              # PyQt6 GUI dashboard
├── features/               # Memory & workflow subsystems
├── gateway/                # WebSocket + HTTP remote control
├── llm/                    # LLM adapters, prompt builder
├── models/                 # Whisper + Piper models (auto-downloaded)
├── orchestration/          # Assistant loop, routing, recovery
├── platform_impl/          # Windows-specific implementations
├── sounds/                 # Alarm audio files
└── tools/                  # Tool registry, executors, code interpreter
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **STT** | faster-whisper (GPU, CUDA) |
| **VAD** | Silero VAD |
| **TTS (English)** | Piper (neural, offline) |
| **TTS (Other)** | gTTS (30+ languages) |
| **LLM** | Ollama (qwen2.5 7B-72B, local) |
| **Vision** | llava (screen understanding) |
| **Browser** | Chrome DevTools Protocol |
| **UI Automation** | pywinauto + pyautogui |
| **Memory** | SQLite |
| **Dashboard** | PyQt6 + QWebEngine |
| **Remote** | WebSocket + HTTP |

## Requirements
- Windows 10/11
- Python 3.12+
- Microphone + speakers
- ~8GB RAM minimum (16GB+ recommended for 14B/32B models)
- GPU recommended (CUDA) for Whisper acceleration
- Internet for weather, news, web features (core works offline)

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
# Windows — use pre-built binary
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
ollama pull qwen2.5:7b
# If behind proxy, set HTTP_PROXY/HTTPS_PROXY
```

**"piper-tts" install fails**
- Piper requires specific Python version compatibility
- Fallback TTS (pyttsx3) works without it — G auto-detects

**Agent mode times out**
- Large models (32B+) need more time — increase `ollama_timeout` in config.json
- Try a smaller model: `ollama pull qwen2.5:7b`

**Vision features not working**
```bash
ollama pull llava
# Verify: ollama list (should show llava)
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

Built with: Ollama, faster-whisper, Silero VAD, Piper TTS, gTTS, PyQt6, pyautogui, pywinauto, and many other open-source libraries.
