#!/usr/bin/env python3
"""
GuestLens — staff roster CSV import for Supabase.

Loads (or re-loads) the client's staff roster from a CSV into the
staff_roster table. Safe to re-run: it REPLACES the whole table each time,
since the CSV is the source of truth and the table is small.

Expected CSV headers (case-insensitive, extra columns ignored):
    first_name, last_name, full_name, aliases,
    locations_can_work_at, department, employment_status

Usage:
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python roster_import.py roster.csv
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python roster_import.py roster.csv --dry-run
"""
from __future__ import annotations

import csv
import sys

import requests

from fetch_supabase import get_supabase_env

COLUMNS = [
    "first_name", "last_name", "full_name", "aliases",
    "locations_can_work_at", "department", "employment_status",
]


def read_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_map = {
            (h or "").strip().lower().replace(" ", "_"): h
            for h in (reader.fieldnames or [])
        }
        missing = [c for c in ("first_name",) if c not in header_map]
        if missing:
            raise SystemExit(
                f"ERROR: CSV is missing required column(s): {missing}\n"
                f"Found headers: {reader.fieldnames}"
            )
        for raw in reader:
            row = {}
            for col in COLUMNS:
                src = header_map.get(col)
                row[col] = (raw.get(src) or "").strip() if src else ""
            if not row["first_name"]:
                continue  # skip blank lines
            if not row["full_name"]:
                row["full_name"] = f"{row['first_name']} {row['last_name']}".strip()
            rows.append(row)
    return rows


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if not args:
        raise SystemExit("Usage: python roster_import.py <roster.csv> [--dry-run]")

    rows = read_csv(args[0])
    print(f"Parsed {len(rows)} staff rows from {args[0]}")
    for r in rows[:5]:
        print(f"  {r['full_name']:<28} locs='{r['locations_can_work_at']}' aliases='{r['aliases']}'")
    if len(rows) > 5:
        print(f"  ... and {len(rows) - 5} more")

    if dry_run:
        print("\n--dry-run: nothing written.")
        return

    url, key = get_supabase_env()
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # Replace-all semantics: the CSV is the source of truth.
    print("Clearing existing staff_roster rows...")
    resp = requests.delete(f"{url}/rest/v1/staff_roster?id=gt.0", headers=headers, timeout=60)
    resp.raise_for_status()

    print(f"Inserting {len(rows)} rows...")
    resp = requests.post(
        f"{url}/rest/v1/staff_roster",
        headers={**headers, "Prefer": "return=minimal"},
        json=rows,
        timeout=120,
    )
    resp.raise_for_status()
    print("Done. Verify in Supabase Studio → staff_roster.")


if __name__ == "__main__":
    main()
