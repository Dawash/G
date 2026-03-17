"""
System health triggers — battery, RAM, disk, network warnings.

These are the highest-urgency triggers; they fire as "speak_now" when critical.
All have cooldowns to prevent spamming the user with repeated warnings.
"""

from __future__ import annotations

from typing import Optional

from core.proactive_engine import BaseTrigger, Suggestion


class BatteryLowTrigger(BaseTrigger):
    """Warns when battery is low and not charging.

    Fires at ≤20 %, escalates to critical at ≤10 %.
    Cooldown: 10 minutes so it doesn't nag on every evaluation cycle.
    """

    id = "battery_low"
    category = "warning"
    cooldown_seconds = 600
    base_urgency = 85

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        battery = state.get("battery_percent", 100)
        charging = state.get("battery_charging", True)

        if charging or battery > 20:
            return None

        if battery <= 10:
            urgency = 95
            msg = (f"Battery critically low at {battery}%. Please plug in now.")
        elif battery <= 15:
            urgency = 90
            msg = (f"Battery at {battery}% and dropping. You should plug in soon.")
        else:
            urgency = self.base_urgency
            msg = (f"Battery at {battery}%. Want me to enable power saver mode?")

        return Suggestion(
            trigger_id=self.id,
            message=msg,
            urgency=urgency,
            category=self.category,
            action="enable_power_saver" if battery > 10 else None,
        )


class HighRAMTrigger(BaseTrigger):
    """Warns when RAM usage is critically high.

    Fires at ≥85 %, escalates urgency at ≥92 %.
    """

    id = "high_ram"
    category = "warning"
    cooldown_seconds = 300
    base_urgency = 70

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        ram = state.get("ram_percent", 0)
        if ram < 85:
            return None

        urgency = 90 if ram >= 92 else 75
        return Suggestion(
            trigger_id=self.id,
            message=(
                f"RAM usage is at {ram:.0f}%. Your system may slow down. "
                "Want me to check what's using the most memory?"
            ),
            urgency=urgency,
            category=self.category,
            action="list_top_processes",
        )


class DiskFullTrigger(BaseTrigger):
    """Warns when disk space is nearly exhausted.

    Fires at ≥90 %, escalates at ≥95 %.
    Cooldown: 1 hour — a full disk doesn't change minute-to-minute.
    """

    id = "disk_full"
    category = "warning"
    cooldown_seconds = 3600
    base_urgency = 75

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        disk = state.get("disk_percent", 0)
        if disk < 90:
            return None

        urgency = 95 if disk >= 95 else 80
        return Suggestion(
            trigger_id=self.id,
            message=(
                f"Disk is {disk:.0f}% full. "
                "Want me to find large files you could clean up?"
            ),
            urgency=urgency,
            category=self.category,
            action="find_large_files",
        )


class NetworkLostTrigger(BaseTrigger):
    """Alerts once when network connectivity is lost.

    Uses edge-detection: fires on the transition from connected → disconnected,
    not on every poll cycle while disconnected.
    """

    id = "network_lost"
    category = "warning"
    cooldown_seconds = 120
    base_urgency = 80

    def __init__(self) -> None:
        super().__init__()
        self._was_connected = True

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        status = state.get("network_status", "connected")

        if status == "connected":
            self._was_connected = True
            return None

        if not self._was_connected:
            return None  # Already reported this disconnect

        self._was_connected = False
        return Suggestion(
            trigger_id=self.id,
            message=(
                "Network connection lost. "
                "Some features may not work until you're back online."
            ),
            urgency=self.base_urgency,
            category=self.category,
        )
