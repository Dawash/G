"""
Dashboard Bridge — QWebChannel communication between Python and JS.

Provides @pyqtSlot methods (JS→Python) and pyqtSignals (Python→JS)
so the Jarvis HTML/JS frontend can interact with the assistant backend.
"""

import json
import logging

from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal

logger = logging.getLogger(__name__)


class DashboardBridge(QObject):
    """Two-way bridge between the Jarvis web UI and Python backend."""

    # ---- Python → JS signals (frontend listens to these) ----
    chatMessageReceived = pyqtSignal(str, str)        # role, text
    systemStatsUpdated = pyqtSignal(dict)              # {cpu, ram, gpu, disk, net_up, net_down}
    agentStatusChanged = pyqtSignal(str, str, str, str)  # id, role, task, status
    thinkingChanged = pyqtSignal(bool)                 # brain is processing
    listeningChanged = pyqtSignal(bool)                # mic is active
    speakingChanged = pyqtSignal(bool)                 # TTS is speaking
    micStateChanged = pyqtSignal(str)                  # IDLE/LISTENING/PROCESSING/SPEAKING
    assistantStateChanged = pyqtSignal(str)            # idle/active
    actionLogUpdated = pyqtSignal(str)                 # JSON list of recent actions

    # ---- Internal signals (bridge → app.py) ----
    userMessageSent = pyqtSignal(str)                  # user typed a message
    voiceToggled = pyqtSignal(bool)                    # mic toggle
    fileDropped = pyqtSignal(str)                      # file/folder path dropped
    weatherRefreshRequested = pyqtSignal()             # JS wants fresh weather/news
    fullscreenToggled = pyqtSignal()                   # F11 fullscreen toggle

    def __init__(self, parent=None):
        super().__init__(parent)
        self._voice_enabled = False

        # Connect Python→JS signals to JS dispatch slots
        self.chatMessageReceived.connect(self._dispatch_chat)
        self.systemStatsUpdated.connect(self._dispatch_stats)
        self.agentStatusChanged.connect(self._dispatch_agent)
        self.thinkingChanged.connect(self._dispatch_thinking)
        self.listeningChanged.connect(self._dispatch_listening)
        self.speakingChanged.connect(self._dispatch_speaking)
        self.micStateChanged.connect(self._dispatch_mic_state)
        self.assistantStateChanged.connect(self._dispatch_assistant_state)
        self.actionLogUpdated.connect(self._dispatch_action_log)

    # ---- JS → Python slots (called from app.js via bridge.sendUserMessage()) ----

    @pyqtSlot(str)
    def sendUserMessage(self, text: str):
        """Called by JS when user submits a chat message."""
        text = text.strip()
        if text:
            logger.info(f"[GUI] User message: {text}")
            self.userMessageSent.emit(text)

    @pyqtSlot(bool)
    def toggleVoice(self, enabled: bool):
        """Called by JS when user toggles the microphone."""
        self._voice_enabled = enabled
        logger.info(f"[GUI] Voice {'enabled' if enabled else 'disabled'}")
        self.voiceToggled.emit(enabled)

    @pyqtSlot(str)
    def processDroppedFile(self, path: str):
        """Called by JS when a file is dropped onto the UI."""
        logger.info(f"[GUI] File dropped: {path}")
        self.fileDropped.emit(path)

    @pyqtSlot(result=bool)
    def isVoiceEnabled(self):
        return self._voice_enabled

    @pyqtSlot()
    def refreshWeather(self):
        """Called by JS to refresh weather + news data."""
        self.weatherRefreshRequested.emit()

    @pyqtSlot()
    def toggleFullscreen(self):
        """Called by JS (F11) to toggle fullscreen."""
        self.fullscreenToggled.emit()

    @pyqtSlot()
    def minimizeWindow(self):
        """Called by JS title bar minimize button."""
        parent = self.parent()
        if parent:
            parent.showMinimized()

    @pyqtSlot()
    def maximizeWindow(self):
        """Called by JS title bar maximize button."""
        parent = self.parent()
        if parent:
            if parent.isMaximized():
                parent.showNormal()
            else:
                parent.showMaximized()

    @pyqtSlot()
    def closeWindow(self):
        """Called by JS title bar close button."""
        parent = self.parent()
        if parent:
            parent.close()

    # ---- Dispatch to JS (run JS functions from Python) ----

    def _run_js(self, js_code: str):
        """Execute JavaScript in the web view."""
        parent = self.parent()
        if parent and hasattr(parent, '_web'):
            parent._web.page().runJavaScript(js_code)

    def _dispatch_chat(self, role: str, text: str):
        safe_text = json.dumps(text)
        safe_role = json.dumps(role)
        self._run_js(f"window.onChatMessage({safe_role}, {safe_text})")

    def _dispatch_stats(self, stats: dict):
        self._run_js(f"window.onStatsUpdate({json.dumps(stats)})")

    def _dispatch_agent(self, agent_id: str, role: str, task: str, status: str):
        data = json.dumps({"id": agent_id, "role": role, "task": task, "status": status})
        self._run_js(f"window.onAgentUpdate({data})")

    def _dispatch_thinking(self, thinking: bool):
        self._run_js(f"window.onThinkingChanged({'true' if thinking else 'false'})")

    def _dispatch_listening(self, listening: bool):
        self._run_js(f"window.onListeningChanged({'true' if listening else 'false'})")

    def _dispatch_speaking(self, speaking: bool):
        self._run_js(f"window.onSpeakingChanged({'true' if speaking else 'false'})")

    def dispatch_config(self, cfg: dict):
        """Push config data (ai_name etc.) to JS."""
        import json as _json
        self._run_js(f"window.onConfigUpdate && window.onConfigUpdate({_json.dumps(cfg)})")

    def dispatch_reminders(self, items: list):
        """Push reminder data to JS."""
        import json as _json
        self._run_js(f"window.onRemindersUpdate && window.onRemindersUpdate({_json.dumps(items)})")

    def dispatch_agent_message(self, agent_id: str, event_type: str, message: str):
        """Push agent inter-communication message to JS."""
        data = json.dumps({"id": agent_id, "type": event_type, "message": message})
        self._run_js(f"window.onAgentMessage && window.onAgentMessage({data})")

    def dispatch_latency(self, seconds: float):
        """Push response latency to JS."""
        self._run_js(f"window.onLatencyUpdate && window.onLatencyUpdate({seconds:.2f})")

    def dispatch_queue_size(self, size: int):
        """Push queue size to JS."""
        self._run_js(f"window.onQueueUpdate && window.onQueueUpdate({size})")

    def dispatch_notification(self, title: str, body: str, ntype: str = "info"):
        """Push a notification to JS notification center."""
        data = json.dumps({"title": title, "body": body, "type": ntype, "ts": __import__('time').time()})
        self._run_js(f"window.onNotification && window.onNotification({data})")

    def dispatch_thinking_step(self, step_type: str, message: str):
        """Push a thinking/reasoning step to JS for the thinking panel."""
        data = json.dumps({"type": step_type, "message": message, "ts": __import__('time').time()})
        self._run_js(f"window.onThinkingStep && window.onThinkingStep({data})")

    def _dispatch_mic_state(self, state: str):
        self._run_js(f"window.onMicStateChanged && window.onMicStateChanged({json.dumps(state)})")

    def _dispatch_assistant_state(self, state: str):
        self._run_js(f"window.onAssistantStateChanged && window.onAssistantStateChanged({json.dumps(state)})")

    def _dispatch_action_log(self, actions_json: str):
        self._run_js(f"window.onActionLogUpdated && window.onActionLogUpdated({actions_json})")
