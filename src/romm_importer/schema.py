"""Small, versioned SQLite migrations for installations created by Phase 1."""

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db import Base


def migrate(engine: Engine) -> None:
    """Create new tables and add Phase-2 columns without discarding Phase-1 data."""
    Base.metadata.create_all(engine)
    additions = {
        "jobs": {
            "dat_snapshot_id": "INTEGER REFERENCES dat_snapshots(id)",
            "tool_versions": "JSON NOT NULL DEFAULT '{}'",
            "publication_completed_at": "DATETIME",
            "scan_task_id": "VARCHAR(128)",
            "scan_status": "VARCHAR(32)",
            "scan_attempts": "INTEGER NOT NULL DEFAULT 0",
            "scan_error": "TEXT",
        },
        "inventory_files": {
            "dat_name": "VARCHAR(512)",
            "dat_version": "VARCHAR(128)",
            "processing_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "proposed_target": "VARCHAR(2048)",
            "is_bios": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "platform_rules": {
            "converter": "VARCHAR(32) NOT NULL DEFAULT 'none'",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            known = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, definition in columns.items():
                if name not in known:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    )
