"""One-time migration: consolidate memory.db + episodic_memory.db -> data/g_memory.db.

Run manually: python -m memory.migration
Or it auto-runs on first import if old DBs exist and new one doesn't.
"""
import os
import shutil
import sqlite3


def migrate():
    """Copy tables from legacy databases into the consolidated database."""
    from core.paths import MEMORY_DB, LEGACY_MEMORY_DB, LEGACY_EPISODIC_DB, DATA_DIR

    os.makedirs(DATA_DIR, exist_ok=True)

    # If consolidated DB already exists and has data, skip
    if os.path.exists(MEMORY_DB) and os.path.getsize(MEMORY_DB) > 0:
        return False

    migrated = False

    # If episodic_memory.db exists, use it as the base (it's newer)
    if os.path.exists(LEGACY_EPISODIC_DB):
        shutil.copy2(LEGACY_EPISODIC_DB, MEMORY_DB)
        migrated = True

    # Merge tables from legacy memory.db that don't exist in consolidated
    if os.path.exists(LEGACY_MEMORY_DB):
        try:
            src = sqlite3.connect(LEGACY_MEMORY_DB)
            dst = sqlite3.connect(MEMORY_DB)
            dst.execute("PRAGMA journal_mode=WAL")

            # Get tables in source
            src_tables = [r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            # Get tables already in destination
            dst_tables = [r[0] for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

            for table in src_tables:
                if table.startswith("sqlite_") or table.endswith("_fts"):
                    continue
                if table not in dst_tables:
                    # Copy entire table schema + data
                    schema = src.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone()
                    if schema and schema[0]:
                        dst.execute(schema[0])
                        rows = src.execute(f"SELECT * FROM [{table}]").fetchall()
                        if rows:
                            placeholders = ",".join(["?"] * len(rows[0]))
                            dst.executemany(
                                f"INSERT INTO [{table}] VALUES ({placeholders})", rows)
                        migrated = True

            dst.commit()
            src.close()
            dst.close()
        except Exception as e:
            print(f"Migration warning: {e}")

    if migrated:
        print(f"[Migration] Consolidated databases -> {MEMORY_DB}")

    return migrated


if __name__ == "__main__":
    migrate()
