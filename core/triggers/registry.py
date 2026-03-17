"""
Trigger registry — imports and registers all built-in triggers.

Call once at startup:
    from core.triggers.registry import register_all_triggers
    count = register_all_triggers()
"""

from __future__ import annotations


def register_all_triggers() -> int:
    """Import and register every built-in trigger with the proactive engine singleton.

    Returns:
        Number of triggers successfully registered.
    """
    from core.proactive_engine import proactive_engine
    from core.triggers.system_triggers import (
        BatteryLowTrigger,
        DiskFullTrigger,
        HighRAMTrigger,
        NetworkLostTrigger,
    )
    from core.triggers.temporal_triggers import (
        EndOfDaySummaryTrigger,
        LateNightTrigger,
        MeetingAlertTrigger,
        MorningBriefingTrigger,
    )
    from core.triggers.context_triggers import (
        AppCrashTrigger,
        ClipboardHelperTrigger,
        IdleDuringWorkTrigger,
        RepetitiveSearchTrigger,
    )
    from core.triggers.pattern_triggers import MorningRoutineTrigger

    triggers = [
        BatteryLowTrigger(),
        HighRAMTrigger(),
        DiskFullTrigger(),
        NetworkLostTrigger(),
        MorningBriefingTrigger(),
        MeetingAlertTrigger(),
        EndOfDaySummaryTrigger(),
        LateNightTrigger(),
        IdleDuringWorkTrigger(),
        RepetitiveSearchTrigger(),
        ClipboardHelperTrigger(),
        AppCrashTrigger(),
        MorningRoutineTrigger(),
    ]

    for trigger in triggers:
        proactive_engine.register_trigger(trigger)

    return len(triggers)
