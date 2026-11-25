#!/usr/bin/env python3
"""Initialize / migrate the database schema.

Usage: python scripts/init_db.py [--db sqlite:///gold_hybrid.db] [--migrations]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.persistence.database import Database, MigrationRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sqlite:///gold_hybrid.db")
    parser.add_argument("--migrations", action="store_true", help="apply SQL migrations instead of create_all")
    args = parser.parse_args()

    database = Database(args.db)
    if args.migrations:
        runner = MigrationRunner(database.engine)
        applied = runner.upgrade()
        print(f"applied migrations: {applied or 'none pending'}")
        print(f"applied so far: {runner.applied()}")
    else:
        database.create_all()
        print("schema created (create_all)")
    print("db healthy:", database.healthy())
    return 0


if __name__ == "__main__":
    sys.exit(main())
