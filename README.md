# G - Personal AI Operating System

A voice-first AI operating system for Windows that listens, understands, and acts. Built from scratch with 42,000+ lines of Python.

G is your personal AI that controls your entire computer through natural voice commands. It opens apps, browses the web, plays music, manages files, automates desktop tasks, and has full conversations - all hands-free.

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
The AI brain powered by Ollama (local, free) with 15 core tools for the 7B model:

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

```
Plan → Observe (screenshot) → Think → Act → Verify → Diagnose if failed
```

- Silent execution - thinks in console, speaks only the result
- Self-healing with web research when stuck
- Voyager-style skill library saves successful action sequences

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
git clone https://github.com/YOUR_USERNAME/G.git
cd G

# Run the auto-launcher (handles everything)
python run.py
```

The launcher automatically:
1. Checks Python version
2. Installs all dependencies
3. Downloads and configures Ollama + qwen2.5:7b model
4. Downloads Whisper STT model
5. Downloads Piper TTS voice
6. Prompts for your name and preferences
7. Starts the assistant

### Manual Installation

```bash
pip install -r requirements.txt

# Install Ollama from https://ollama.ai
ollama pull qwen2.5:7b

# Run
python main.py
```

### First Run Setup
On first run, G will ask you:
1. **Your name** - Used for personalized greetings
2. **AI name** - What you want to call your AI (default: "G")
3. **AI provider** - Ollama (local, free), OpenAI, Anthropic, or OpenRouter
4. **API key** - Only needed for cloud providers (encrypted on disk)

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
| **Ollama** (default) | qwen2.5:7b | Free (local) | `ollama pull qwen2.5:7b` |
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
"Play a good song"              → Plays popular music on Spotify
"Play Shape of You"             → Plays specific song
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

### Tutorial 8: Multilingual
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
└── tools/                  # Tool registry, executors, audit
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **STT** | faster-whisper (GPU, CUDA) |
| **VAD** | Silero VAD |
| **TTS (English)** | Piper (neural, offline) |
| **TTS (Other)** | gTTS (30+ languages) |
| **LLM** | Ollama (qwen2.5:7b, local) |
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
- ~4GB RAM for Ollama model
- GPU recommended (CUDA) for Whisper acceleration
- Internet for weather, news, web features (core works offline)

## License

MIT License - see [LICENSE](LICENSE)

## Credits

Created by **Dawa Sangay Sherpa** as a personal AI operating system project.

Built with: Ollama, faster-whisper, Silero VAD, Piper TTS, gTTS, PyQt6, pyautogui, pywinauto, and many other open-source libraries.
