"""
User-facing memory controls — remember, forget, recall, private mode.

Handles the logic for memory_control tool actions.
"""

import json
import logging

logger = logging.getLogger(__name__)


def handle_memory_command(action, data, memory_store, preferences=None):
    """Execute a memory control command.

    Args:
        action: remember | forget | recall | search | private_on | private_off | preferences
        data: context-dependent string (fact to remember, key to forget, search query)
        memory_store: MemoryStore instance
        preferences: UserPreferences instance (optional)

    Returns:
        str: Human-readable result.
    """
    action = action.lower().strip()

    if action == "remember":
        return _handle_remember(data, memory_store)
    elif action == "forget":
        return _handle_forget(data, memory_store)
    elif action == "recall":
        return _handle_recall(data, memory_store)
    elif action == "search":
        return _handle_search(data, memory_store)
    elif action == "private_on":
        memory_store.set_private_mode(True)
        return "Private mode enabled. I won't log actions or events until you turn it off."
    elif action == "private_off":
        memory_store.set_private_mode(False)
        return "Private mode disabled. Resuming normal logging."
    elif action == "preferences":
        return _handle_preferences(data, preferences)
    else:
        return f"Unknown memory action: {action}"


def _handle_remember(data, store):
    """Parse and store a fact. Expects 'key: value' or natural phrasing."""
    if not data:
        return "What should I remember?"

    # Try "key: value" format
    if ":" in data:
        key, value = data.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
    elif " is " in data.lower():
        idx = data.lower().index(" is ")
        key = data[:idx].strip().lower()
        value = data[idx + 4:].strip()
    elif " are " in data.lower():
        idx = data.lower().index(" are ")
        key = data[:idx].strip().lower()
        value = data[idx + 5:].strip()
    else:
        key = "fact"
        value = data.strip()

    # Clean common prefixes
    for prefix in ("that ", "my ", "i ", "the "):
        if key.startswith(prefix):
            key = key[len(prefix):]

    store.remember("facts", key, value)
    return f"Got it, I'll remember that {key} is {value}."


def _handle_forget(data, store):
    """Remove a memory by key or category."""
    if not data:
        return "What should I forget?"

    data_lower = data.lower().strip()

    if data_lower in ("everything", "all", "all memories"):
        count = store.count_memories()
        store.forget_category("facts")
        return f"I've forgotten all stored facts ({count} memories cleared)."

    if data_lower.startswith("all ") and data_lower[4:]:
        category = data_lower[4:].strip()
        store.forget_category(category)
        return f"I've forgotten all {category} memories."

    # Try to find and remove by key
    for prefix in ("about ", "my ", "that ", "the "):
        if data_lower.startswith(prefix):
            data_lower = data_lower[len(prefix):]

    # Search in facts first, then other categories
    for category in ("facts", "preferences", "nicknames", "learned"):
        existing = store.recall(category, data_lower)
        if existing:
            store.forget(category, data_lower)
            return f"Forgotten: {data_lower} (was: {existing})."

    # Try search
    results = store.search(data_lower, limit=1)
    if results:
        r = results[0]
        store.forget(r["category"], r["key"])
        return f"Forgotten: {r['key']} (was: {r['value']})."

    return f"I don't have any memory matching '{data}'."


def _handle_recall(data, store):
    """List what's remembered — all facts or filtered."""
    facts = store.get_all_facts()
    if not facts:
        return "I don't have any stored memories yet."

    if data:
        # Filter by keyword
        results = store.search(data.strip(), limit=10)
        if not results:
            return f"I don't remember anything about '{data}'."
        lines = []
        for r in results:
            lines.append(f"- {r['key']}: {r['value']}")
        return f"Here's what I know about '{data}':\n" + "\n".join(lines)

    # List all
    lines = []
    total = 0
    for category, items in facts.items():
        if category in ("preferences",):
            continue  # Skip internal tracking
        lines.append(f"\n**{category.title()}:**")
        for item in items[:10]:
            lines.append(f"  - {item['key']}: {item['value']}")
            total += 1
        if len(items) > 10:
            lines.append(f"  ... and {len(items) - 10} more")
            total += len(items) - 10

    if not lines:
        return "I don't have any stored memories yet."
    return f"I remember {total} things:" + "\n".join(lines)


def _handle_search(data, store):
    """Search memories by keyword."""
    if not data:
        return "What should I search for?"
    results = store.search(data.strip(), limit=10)
    if not results:
        return f"No memories matching '{data}'."
    lines = []
    for r in results:
        lines.append(f"- [{r['category']}] {r['key']}: {r['value']}")
    return f"Found {len(results)} result(s):\n" + "\n".join(lines)


def _handle_preferences(data, preferences):
    """Show or set preferences."""
    if preferences is None:
        return "Preference system not available."

    if not data or data.lower().strip() in ("show", "list", "all"):
        prefs = preferences.get_all_preferences()
        lines = []
        for k, v in sorted(prefs.items()):
            lines.append(f"  - {k}: {v}")
        return "Your preferences:\n" + "\n".join(lines)

    # Set: "response_style: concise" or "response_style concise"
    if ":" in data:
        key, value = data.split(":", 1)
    elif " " in data.strip():
        parts = data.strip().rsplit(" ", 1)
        key, value = parts[0], parts[1]
    else:
        return f"Usage: set preference key: value"

    key = key.strip().lower().replace(" ", "_")
    value = value.strip().lower()
    preferences.set_preference(key, value)
    return f"Preference '{key}' set to '{value}'."
