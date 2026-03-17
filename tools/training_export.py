"""
Training data exporter for LoRA fine-tuning.

Extracts command→action pairs from:
  - usage_log in memory.db (real user interactions)
  - skill library (successful tool sequences)
  - test commands from self_test.py and test_core.py

Outputs in JSONL format compatible with:
  - Ollama fine-tuning (modelfile FROM + ADAPTER)
  - Hugging Face LoRA (alpaca/sharegpt format)
  - OpenAI fine-tuning API

Usage:
    python -m tools.training_export --output training_data.jsonl
    python -m tools.training_export --format sharegpt --output train.json
"""

import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

try:
    from core.paths import MEMORY_DB as _DEFAULT_DB
except ImportError:
    _DEFAULT_DB = "memory.db"

# Manual seed examples — high-quality command→tool mappings
_SEED_EXAMPLES = [
    {"input": "what time is it", "tool": "get_time", "args": {}},
    {"input": "what's the weather", "tool": "get_weather", "args": {}},
    {"input": "weather in tokyo", "tool": "get_weather", "args": {"city": "tokyo"}},
    {"input": "open chrome", "tool": "open_app", "args": {"name": "chrome"}},
    {"input": "close notepad", "tool": "close_app", "args": {"name": "notepad"}},
    {"input": "play some jazz", "tool": "play_music", "args": {"query": "jazz", "action": "play"}},
    {"input": "play jazz on spotify", "tool": "play_music", "args": {"query": "jazz", "app": "spotify"}},
    {"input": "set a reminder for 5pm to call mom", "tool": "set_reminder", "args": {"time": "5pm", "message": "call mom"}},
    {"input": "turn on dark mode", "tool": "toggle_setting", "args": {"setting": "dark mode", "state": "on"}},
    {"input": "turn off bluetooth", "tool": "toggle_setting", "args": {"setting": "bluetooth", "state": "off"}},
    {"input": "how much ram do i have", "tool": "run_terminal", "args": {"command": "systeminfo | findstr Memory"}},
    {"input": "check my battery", "tool": "run_terminal", "args": {"command": "WMIC PATH Win32_Battery Get EstimatedChargeRemaining"}},
    {"input": "check my disk space", "tool": "run_terminal", "args": {"command": "Get-PSDrive C,D"}},
    {"input": "take a screenshot", "tool": "take_screenshot", "args": {}},
    {"input": "get me the news", "tool": "get_news", "args": {"category": "general"}},
    {"input": "search for python tutorials", "tool": "google_search", "args": {"query": "python tutorials"}},
    {"input": "install firefox", "tool": "manage_software", "args": {"action": "install", "name": "firefox"}},
    {"input": "volume up", "tool": "play_music", "args": {"action": "volume_up"}},
    {"input": "mute", "tool": "play_music", "args": {"action": "mute"}},
    {"input": "next song", "tool": "play_music", "args": {"action": "next"}},
    {"input": "book a flight to paris", "tool": "agent_task", "args": {"goal": "Navigate to Google Flights and search for flights to Paris"}},
    {"input": "order pizza from dominos", "tool": "agent_task", "args": {"goal": "Navigate to dominos.com and help user order pizza"}},
    {"input": "open youtube and play lofi", "tool": "agent_task", "args": {"goal": "Open YouTube and play lofi hip hop music"}},
    # Chat/knowledge — no tool needed
    {"input": "hello", "tool": None, "response": "Hello! How can I help you today?"},
    {"input": "who are you", "tool": None, "response": "I'm G, a personal AI assistant created by Dawa Sangay Sherpa."},
    {"input": "what is the capital of france", "tool": None, "response": "The capital of France is Paris."},
    {"input": "tell me a joke", "tool": None, "response": "Why don't scientists trust atoms? Because they make up everything!"},
    {"input": "how are you", "tool": None, "response": "I'm doing great, ready to help! What can I do for you?"},
]


def export_from_usage_log(db_path=_DEFAULT_DB, limit=500):
    """Extract command→action pairs from usage_log in memory.db."""
    examples = []
    if not os.path.exists(db_path):
        return examples
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT action, entity, COUNT(*) as cnt
            FROM usage_log
            WHERE action IS NOT NULL AND entity IS NOT NULL
            GROUP BY action, entity
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,))
        for row in c.fetchall():
            action = row["action"]
            entity = row["entity"]
            if action and entity:
                examples.append({
                    "input": f"{action} {entity}".replace("_", " "),
                    "tool": action,
                    "args": {"name": entity} if action in ("open_app", "close_app") else {"query": entity},
                    "frequency": row["cnt"],
                })
        conn.close()
    except Exception as e:
        logger.warning(f"Usage log export failed: {e}")
    return examples


def export_from_skills(db_path=_DEFAULT_DB):
    """Extract successful skill sequences from skill library."""
    examples = []
    if not os.path.exists(db_path):
        return examples
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT goal, tool_sequence, success_count
            FROM skills
            WHERE success_count > 0
            ORDER BY success_count DESC
            LIMIT 100
        """)
        for row in c.fetchall():
            try:
                sequence = json.loads(row["tool_sequence"])
                if sequence:
                    first_tool = sequence[0]
                    examples.append({
                        "input": row["goal"],
                        "tool": first_tool.get("tool", "agent_task"),
                        "args": first_tool.get("args", {}),
                        "frequency": row["success_count"],
                    })
            except (json.JSONDecodeError, IndexError):
                pass
        conn.close()
    except Exception as e:
        logger.warning(f"Skill export failed: {e}")
    return examples


def build_training_data(format="alpaca"):
    """Build complete training dataset.

    Args:
        format: "alpaca" (instruction/input/output) or "sharegpt" (conversations)

    Returns:
        list of training examples
    """
    all_examples = []

    # Seed examples (handcrafted, high quality)
    all_examples.extend(_SEED_EXAMPLES)

    # Usage log examples (real user data)
    usage = export_from_usage_log()
    all_examples.extend(usage)

    # Skill library examples
    skills = export_from_skills()
    all_examples.extend(skills)

    # Deduplicate by input
    seen = set()
    unique = []
    for ex in all_examples:
        key = ex["input"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(ex)

    # Format for training
    if format == "sharegpt":
        return _format_sharegpt(unique)
    return _format_alpaca(unique)


def _format_alpaca(examples):
    """Format as Alpaca-style instruction/output pairs."""
    formatted = []
    for ex in examples:
        if ex.get("tool"):
            output = json.dumps({"tool": ex["tool"], "args": ex.get("args", {})})
        else:
            output = ex.get("response", "I can help with that.")
        formatted.append({
            "instruction": "You are G, a personal AI assistant. Decide whether to use a tool or respond directly.",
            "input": ex["input"],
            "output": output,
        })
    return formatted


def _format_sharegpt(examples):
    """Format as ShareGPT-style conversations."""
    formatted = []
    for ex in examples:
        if ex.get("tool"):
            assistant_msg = json.dumps({"tool": ex["tool"], "args": ex.get("args", {})})
        else:
            assistant_msg = ex.get("response", "I can help with that.")
        formatted.append({
            "conversations": [
                {"from": "system", "value": "You are G, a personal AI assistant with access to system tools."},
                {"from": "human", "value": ex["input"]},
                {"from": "gpt", "value": assistant_msg},
            ]
        })
    return formatted


def save_training_data(output_path="training_data.jsonl", format="alpaca"):
    """Export training data to JSONL file."""
    data = build_training_data(format=format)
    with open(output_path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logger.info(f"Training data exported: {len(data)} examples to {output_path}")
    return len(data)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Export training data for LoRA fine-tuning")
    parser.add_argument("--output", default="training_data.jsonl", help="Output file path")
    parser.add_argument("--format", choices=["alpaca", "sharegpt"], default="alpaca")
    args = parser.parse_args()

    count = save_training_data(args.output, args.format)
    print(f"Exported {count} training examples to {args.output}")
