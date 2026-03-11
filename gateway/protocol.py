"""
Message protocol for the WebSocket gateway.

Client -> Server:
  {"id": "uuid", "type": "auth", "token": "secret"}
  {"id": "uuid", "type": "think", "text": "open chrome"}
  {"id": "uuid", "type": "quick_chat", "text": "tell me a joke"}
  {"id": "uuid", "type": "tool", "name": "get_weather", "args": {"city": "NYC"}}
  {"id": "uuid", "type": "status"}

Server -> Client:
  {"id": "uuid", "type": "response", "text": "...", "ok": true}
  {"id": "uuid", "type": "error", "text": "...", "ok": false}
  {"type": "event", "event": "speaking", "text": "..."}
  {"type": "event", "event": "tool_executed", "tool": "...", "result": "..."}
  {"type": "event", "event": "listening"}
"""

import json
import uuid


def make_id():
    return uuid.uuid4().hex[:12]


def response_msg(msg_id, text, ok=True):
    return json.dumps({"id": msg_id, "type": "response", "text": str(text), "ok": ok})


def error_msg(msg_id, text):
    return json.dumps({"id": msg_id, "type": "error", "text": str(text), "ok": False})


def event_msg(event, **data):
    return json.dumps({"type": "event", "event": event, **data})


def parse_message(raw):
    """Parse incoming message. Returns dict or None."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
