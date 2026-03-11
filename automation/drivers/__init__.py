"""
App-specific drivers — structured knowledge about how to operate specific apps.

Phase 20: Each driver knows the capabilities, controls, and common operations
for a specific application. Drivers provide:
  - Preconditions (what must be true before an action)
  - Action implementations (how to do things in this app)
  - Postconditions (how to verify success)
  - Common keyboard shortcuts

Available drivers:
  - browser.py: Chrome/Edge/Firefox operations
  - explorer.py: File Explorer operations
  - settings.py: Windows Settings operations
"""

# Auto-import drivers so they self-register
from automation.drivers import browser    # noqa: F401
from automation.drivers import explorer   # noqa: F401
from automation.drivers import settings   # noqa: F401

from automation.drivers.base import (     # noqa: F401
    get_driver_for,
    get_driver_by_name,
    list_drivers,
)
