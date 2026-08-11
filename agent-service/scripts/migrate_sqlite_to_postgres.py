#!/usr/bin/env python3
"""One-time, non-destructive migration from the development SQLite store."""

import argparse
import os
import sys
from typing import Dict, List

from app.main import AgentStore


TABLES = [
    "agents", "agent_versions", "conversations", "runs", "messages", "trace_events",
    "tools", "agent_tools", "run_snapshots", "tool_confirmations", "memory_items",
    "evaluation_cases", "evaluation_runs", "evaluation_results", "outbox_events",
]


def postgres_columns(store: AgentStore, table: str) -> List[str]:
    rows = store.db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ? "
        "ORDER BY ordinal_position", (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def migrate(source_path: str, target_url: str, master_key: str) -> Dict[str, int]:
    source = AgentStore(source_path, master_key)
    target = AgentStore(target_url, master_key)
    counts: Dict[str, int] = {}
    try:
        occupied = [table for table in TABLES if target.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]]
        if occupied:
            raise RuntimeError("target database is not empty: " + ", ".join(occupied))
        for table in TABLES:
            target_columns = postgres_columns(target, table)
            source_columns = {row[1] for row in source.db.execute("PRAGMA table_info(" + table + ")").fetchall()}
            columns = [column for column in target_columns if column in source_columns]
            rows = source.db.execute("SELECT " + ", ".join(columns) + " FROM " + table).fetchall()
            if rows:
                sql = "INSERT INTO {} ({}) VALUES ({})".format(
                    table, ", ".join(columns), ", ".join("?" for _ in columns),
                )
                for row in rows:
                    target.db.execute(sql, tuple(row[column] for column in columns))
            counts[table] = len(rows)
        target.db.commit()
    except Exception:
        target.db.rollback()
        raise
    finally:
        source.db.close()
        target.db.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Path to the existing agents.db (never modified or deleted)")
    args = parser.parse_args()
    target_url = os.environ.get("AGENT_DATABASE_URL", "")
    master_key = os.environ.get("AGENT_MASTER_KEY", "")
    if not target_url.startswith(("postgresql://", "postgres://")) or not master_key:
        parser.error("AGENT_DATABASE_URL and AGENT_MASTER_KEY must be set")
    if not os.path.isfile(args.source):
        parser.error("source SQLite file does not exist")
    try:
        counts = migrate(args.source, target_url, master_key)
    except Exception as exc:
        print("migration failed: {}".format(exc), file=sys.stderr)
        return 1
    for table, count in counts.items():
        print("{}: {} rows".format(table, count))
    print("migration completed; source file was retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
