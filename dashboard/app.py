"""
Jarvis Dashboard — PyQt6 frameless workspace HUD.

Voice-first: auto-starts voice on launch.
Workspace layout: all panels visible at once (no floating windows).
Pushes weather/news data to JS on startup.
Interruptible TTS: barge-in stops speaking when user talks.
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import sys
import threading

from PyQt6.QtCore import Qt, QUrl, QPoint, QTimer
from PyQt6.QtGui import QPalette, QColor, QIcon, QPixmap, QPainter
from PyQt6.QtWidgets import (QMainWindow, QApplication, QVBoxLayout, QWidget,
                              QSystemTrayIcon, QMenu)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel

from dashboard.bridge import DashboardBridge
from dashboard.system_monitor import SystemMonitorThread
from dashboard.brain_thread import BrainThread
from dashboard.voice_thread import VoiceThread

logger = logging.getLogger(__name__)
UI_DIR = os.path.join(os.path.dirname(__file__), "ui")


class JarvisDashboard(QMainWindow):
    """Frameless Jarvis workspace HUD — voice-first."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("G — Jarvis OS")
        self.setMinimumSize(1100, 650)
        self.resize(1400, 850)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet("QMainWindow { background: #020810; }")

        # --- Web view ---
        self._web = QWebEngineView()
        s = self._web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self._web.setStyleSheet("background: #020810;")

        # --- Bridge ---
        self._bridge = DashboardBridge(self)
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self._web.page().setWebChannel(self._channel)

        # --- Load HTML ---
        self._web.setUrl(QUrl.fromLocalFile(os.path.join(UI_DIR, "index.html")))

        # Central widget
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._web)
        self.setCentralWidget(container)

        # --- Threads ---
        self._monitor = SystemMonitorThread()
        self._monitor.statsUpdated.connect(self._on_stats)
        self._monitor.start()

        self._brain_thread = BrainThread()
        self._brain_thread.responseReady.connect(self._on_brain_response)
        self._brain_thread.agentSpawned.connect(self._on_agent_spawned)
        self._brain_thread.agentFinished.connect(self._on_agent_finished)
        self._brain_thread.agentMessage.connect(self._on_agent_message)
        self._brain_thread.thinkingChanged.connect(self._on_thinking_changed)
        self._brain_thread.latencyUpdated.connect(self._on_latency)
        self._brain_thread.queueSizeChanged.connect(self._on_queue_size)
        self._brain_thread.thinkingStep.connect(self._on_thinking_step)
        self._brain_thread.start()

        self._voice_thread = VoiceThread()
        self._voice_thread.transcriptionReady.connect(self._on_transcription)
        self._voice_thread.listeningChanged.connect(self._on_listening_changed)
        self._voice_thread.speakingChanged.connect(self._on_speaking_changed)

        # Wire bridge signals
        self._bridge.userMessageSent.connect(self._handle_user_message)
        self._bridge.voiceToggled.connect(self._toggle_voice)
        self._bridge.fileDropped.connect(self._handle_file_drop)
        self._bridge.weatherRefreshRequested.connect(self._refresh_weather)
        self._bridge.fullscreenToggled.connect(self._toggle_fullscreen)

        # Drag state
        self._drag_pos = QPoint()
        self.setAcceptDrops(True)

        # Always-on mic: auto-start voice on page load
        self._voice_auto_started = False

        # Push weather + news after page loads
        self._web.loadFinished.connect(self._on_page_loaded)

        # Load config for dynamic branding
        self._config = self._load_config()

        # Set window title from config
        ai_name = self._config.get("ai_name", "G")
        self.setWindowTitle(f"{ai_name} — AI Operating System")

        # Reminder refresh timer (every 30s)
        self._reminder_timer = QTimer()
        self._reminder_timer.timeout.connect(self._push_reminders)
        self._reminder_timer.start(30000)

        # System tray icon
        self._setup_tray()

        # Global hotkey (Win+G to toggle visibility)
        self._setup_global_hotkey()

        # Multi-monitor: position on secondary if available
        self._position_on_screen()

    # ---- Always-on mic ----

    def _auto_start_voice(self):
        """Start voice thread immediately for background listening."""
        if not self._voice_auto_started and not self._voice_thread.isRunning():
            self._voice_auto_started = True
            self._voice_thread.start()
            logger.info("Voice auto-started (always-on background mic)")

    # ---- Config ----

    def _load_config(self) -> dict:
        """Load config.json for branding info."""
        try:
            cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Config load failed: {e}")
        return {}

    # ---- Data push on page load ----

    def _on_page_loaded(self, ok):
        if not ok:
            return
        # Push config (ai_name etc.) to JS for dynamic branding
        ai_name = self._config.get("ai_name", "G")
        self._run_js(f"window.onConfigUpdate && window.onConfigUpdate({json.dumps({'ai_name': ai_name})})")
        # Push weather, news, and reminders in background
        threading.Thread(target=self._push_initial_data, daemon=True).start()
        self._push_reminders()
        # Auto-start voice (always-on background mic)
        QTimer.singleShot(2000, self._auto_start_voice)

    def _push_initial_data(self):
        """Fetch weather + news and push to JS."""
        # Weather
        try:
            from weather import get_current_weather, get_forecast
            current = get_current_weather()
            forecast_text = get_forecast()
            if current:
                # Parse weather text into structured data for JS
                weather_data = {"temp": "—", "description": current, "unit": "C",
                                "humidity": "—", "wind": "—", "feels_like": "—",
                                "location": "", "forecast": []}
                self._run_js(f"window.onWeatherUpdate && window.onWeatherUpdate({json.dumps(weather_data)})")
        except Exception as e:
            logger.warning(f"Weather push failed: {e}")

        # News
        try:
            from news import get_briefing
            briefing = get_briefing("general")
            if briefing:
                # Split into headlines
                lines = [l.strip() for l in str(briefing).split('\n') if l.strip() and not l.startswith('Here')]
                items = [{"title": l.lstrip('•-123456789. ')} for l in lines[:6]]
                self._run_js(f"window.onNewsUpdate && window.onNewsUpdate({json.dumps(items)})")
        except Exception as e:
            logger.warning(f"News push failed: {e}")

    def _run_js(self, code: str):
        """Run JS in the web view (thread-safe via QTimer)."""
        QTimer.singleShot(0, lambda: self._web.page().runJavaScript(code))

    def _refresh_weather(self):
        """Re-fetch weather + news data (called from JS every 30 min)."""
        threading.Thread(target=self._push_initial_data, daemon=True).start()

    def _toggle_fullscreen(self):
        """Toggle between fullscreen and normal window mode."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _push_reminders(self):
        """Push active reminders to JS for countdown display."""
        try:
            import time as _time
            reminders_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reminders.json")
            if not os.path.exists(reminders_path):
                return
            with open(reminders_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = []
            now = _time.time()
            for r in data if isinstance(data, list) else data.get("reminders", []):
                # Calculate seconds until due
                due_ts = r.get("timestamp", 0) or r.get("due", 0)
                due_in = max(0, int(due_ts - now)) if due_ts > 0 else 0
                items.append({
                    "text": r.get("text", r.get("message", "")),
                    "due_in_seconds": due_in,
                    "time": r.get("time_str", "")
                })
            if items:
                self._bridge.dispatch_reminders(items)
        except Exception as e:
            logger.debug(f"Reminders push: {e}")

    # ---- Signal handlers ----

    def _on_stats(self, stats: dict):
        self._bridge.systemStatsUpdated.emit(stats)

    def _on_brain_response(self, role: str, text: str):
        # Skip internal init commands
        if text.startswith('__INIT_'):
            return
        self._bridge.chatMessageReceived.emit(role, text)
        # Speak assistant responses if voice is active (interruptible TTS)
        if role == "assistant" and self._voice_thread.isRunning():
            self._voice_thread.speak(text)

    def _on_agent_spawned(self, agent_id: str, role: str, task: str):
        self._bridge.agentStatusChanged.emit(agent_id, role, task, "working")

    def _on_agent_finished(self, agent_id: str, role: str, task: str, status: str):
        self._bridge.agentStatusChanged.emit(agent_id, role, task, status)

    def _on_thinking_changed(self, thinking: bool):
        self._bridge.thinkingChanged.emit(thinking)

    def _on_agent_message(self, agent_id: str, event_type: str, message: str):
        self._bridge.dispatch_agent_message(agent_id, event_type, message)

    def _on_latency(self, seconds: float):
        self._bridge.dispatch_latency(seconds)

    def _on_queue_size(self, size: int):
        self._bridge.dispatch_queue_size(size)

    def _on_thinking_step(self, step_type: str, message: str):
        self._bridge.dispatch_thinking_step(step_type, message)

    def _on_transcription(self, text: str):
        self._bridge.chatMessageReceived.emit("user", text)
        self._brain_thread.enqueue(text)

    def _on_listening_changed(self, listening: bool):
        self._bridge.listeningChanged.emit(listening)

    def _on_speaking_changed(self, speaking: bool):
        self._bridge.speakingChanged.emit(speaking)

    def _handle_user_message(self, text: str):
        # Skip internal init messages
        if text.startswith('__INIT_'):
            return
        self._bridge.chatMessageReceived.emit("user", text)
        self._brain_thread.enqueue(text)

    def _toggle_voice(self, enabled: bool):
        if enabled and not self._voice_thread.isRunning():
            self._voice_thread.start()
        elif not enabled and self._voice_thread.isRunning():
            self._voice_thread.stop_listening()

    def _handle_file_drop(self, path: str):
        from dashboard.drop_handler import DropHandler
        handler = DropHandler(self._brain_thread)
        handler.process(path, lambda role, text: self._bridge.chatMessageReceived.emit(role, text))

    # ---- System tray ----

    def _setup_tray(self):
        """Create system tray icon with context menu."""
        try:
            # Create a simple icon (cyan circle)
            pix = QPixmap(32, 32)
            pix.fill(QColor(0, 0, 0, 0))
            p = QPainter(pix)
            p.setBrush(QColor("#00d4ff"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(2, 2, 28, 28)
            p.end()

            self._tray = QSystemTrayIcon(QIcon(pix), self)
            menu = QMenu()
            show_action = menu.addAction("Show / Hide")
            show_action.triggered.connect(self._toggle_visibility)
            quit_action = menu.addAction("Quit")
            quit_action.triggered.connect(self.close)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(self._on_tray_click)
            self._tray.show()
            ai_name = self._config.get("ai_name", "G")
            self._tray.setToolTip(f"{ai_name} — AI Operating System")
        except Exception as e:
            logger.warning(f"Tray setup failed: {e}")

    def _on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()

    # ---- Global hotkey (Win+G) ----

    def _setup_global_hotkey(self):
        """Register Win+G as a global hotkey to toggle visibility."""
        try:
            def hotkey_listener():
                MOD_WIN = 0x0008
                VK_G = 0x47
                HOTKEY_ID = 1
                ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, MOD_WIN, VK_G)
                msg = ctypes.wintypes.MSG()
                while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:
                        QTimer.singleShot(0, self._toggle_visibility)
                ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)

            self._hotkey_thread = threading.Thread(target=hotkey_listener, daemon=True)
            self._hotkey_thread.start()
            logger.info("Global hotkey Win+G registered")
        except Exception as e:
            logger.warning(f"Global hotkey failed: {e}")

    # ---- Multi-monitor ----

    def _position_on_screen(self):
        """If multiple monitors, position on secondary screen."""
        try:
            screens = QApplication.screens()
            if len(screens) > 1:
                secondary = screens[1]
                geo = secondary.availableGeometry()
                self.move(geo.x() + 50, geo.y() + 50)
                self.resize(min(self.width(), geo.width() - 100),
                            min(self.height(), geo.height() - 100))
                logger.info(f"Positioned on secondary monitor: {secondary.name()}")
        except Exception as e:
            logger.debug(f"Multi-monitor: {e}")

    # ---- Window drag ----

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self._drag_pos.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = QPoint()

    # ---- Cleanup ----

    def closeEvent(self, event):
        self._monitor.stop()
        self._brain_thread.stop()
        if self._voice_thread.isRunning():
            self._voice_thread.stop_listening()
        # Unregister hotkey
        try:
            ctypes.windll.user32.UnregisterHotKey(None, 1)
        except Exception:
            pass
        # Hide tray
        if hasattr(self, '_tray'):
            self._tray.hide()
        event.accept()


def apply_dark_palette(app: QApplication):
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#020810"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e0f0ff"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0a0e17"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e0f0ff"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#0a0e17"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e0f0ff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#00d4ff"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    app.setPalette(palette)
