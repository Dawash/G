"""Advanced 3-layer memory system: working, episodic, semantic.

Also re-exports MemoryStore, UserPreferences, HabitTracker from the legacy
memory.py module so that existing code (assistant_loop.py, brain.py, etc.)
continues to work unchanged after this directory became a package.
"""

import importlib.util as _ilu
import os as _os

# Load the legacy memory.py (same parent dir) without triggering a circular import.
# Both memory/ (this package) and memory.py coexist; Python prefers the package,
# so we must load the .py file explicitly.
_legacy_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "memory.py")
if _os.path.isfile(_legacy_path):
    _spec = _ilu.spec_from_file_location("_memory_legacy", _legacy_path)
    _legacy = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_legacy)
    MemoryStore = _legacy.MemoryStore
    UserPreferences = _legacy.UserPreferences
    HabitTracker = _legacy.HabitTracker
    # Make the legacy module importable as _memory_legacy for downstream usage
    import sys as _sys
    _sys.modules.setdefault("_memory_legacy", _legacy)
    del _spec, _legacy
else:
    # Fallback stubs so imports never hard-crash if the file is later removed
    class MemoryStore:         # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
    class UserPreferences:     # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
    class HabitTracker:        # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass

del _ilu, _legacy_path

# Auto-migrate legacy databases on first use
try:
    from core.paths import MEMORY_DB as _new_db, LEGACY_MEMORY_DB as _old1, LEGACY_EPISODIC_DB as _old2
    if not _os.path.exists(_new_db) and (_os.path.exists(_old1) or _os.path.exists(_old2)):
        from memory.migration import migrate
        migrate()
    del _new_db, _old1, _old2
except Exception:
    pass

del _os
