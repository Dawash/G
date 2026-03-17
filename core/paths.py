"""Centralized file and database paths for Project G."""
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

# Consolidated database — all memory, skills, failures in one place
MEMORY_DB = os.path.join(DATA_DIR, "g_memory.db")

# Legacy paths (for migration)
LEGACY_MEMORY_DB = os.path.join(PROJECT_DIR, "memory.db")
LEGACY_EPISODIC_DB = os.path.join(DATA_DIR, "episodic_memory.db")
