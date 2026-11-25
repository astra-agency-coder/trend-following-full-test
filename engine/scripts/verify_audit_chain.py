#!/usr/bin/env python3
"""Verify the append-only audit chain (spec 12.3 / 49.5-8).

Usage: python scripts/verify_audit_chain.py [--db sqlite:///gold_hybrid.db]
Exits 0 if intact, 1 if tampered.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.journal.audit import AuditJournal
from app.persistence.database import Database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sqlite:///gold_hybrid.db")
    args = parser.parse_args()

    database = Database(args.db)
    journal = AuditJournal(database)
    count = journal.count()
    intact = journal.verify_chain()
    if intact:
        print(f"OK: audit chain intact ({count} events)")
        return 0
    print(f"TAMPERED: audit chain broken ({count} events)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
