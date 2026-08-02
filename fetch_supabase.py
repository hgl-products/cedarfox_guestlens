#!/usr/bin/env python3
"""
GuestLens — Supabase fetch layer.

Replaces build_airtable.py's get_pat()/fetch_all_records() for Supabase-native
clients. Each fetch_* function returns records in the EXACT Airtable shape the
battle-tested compute pipeline expects:

    [{"id": "<row identifier>", "fields": {"Airtable_Style_Name": value, ...}}]

so build_v3.py, build_airtable.py's compute functions, and staff_roster.py
run unchanged. This file is client-agnostic.

Env vars (set locally and as GitHub Actions secrets):
    SUPABASE_URL          https://<project-ref>.supabase.co
    SUPABASE_SERVICE_KEY  service_role key (bypasses RLS — keep secret)
"""
from __future__ import annotations

import os
from typing import Any

import requests

PAGE_SIZE = 1000  # PostgREST default max rows per request


# ---------- auth / env -----------------------------------------------------------

def get_supabase_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise SystemExit(
            "ERROR: SUPABASE_URL and/or SUPABASE_SERVICE_KEY are not set.\n"
            "Set them with:\n"
            "  export SUPABASE_URL=https://xxxx.supabase.co\n"
            "  export SUPABASE_SERVICE_KEY=eyJ..."
        )
    return url, key


# ---------- generic paginated fetch ----------------------------------------------

def fetch_all_rows(table: str, select: str = "*", filters: dict[str, str] | None = None) -> list[dict]:
    """Fetch every row from a table via PostgREST, paginating with Range headers."""
    url, key = get_supabase_env()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    params: dict[str, Any] = {"select": select, "order": "id.asc"}
    if filters:
        params.update(filters)

    rows: list[dict] = []
    start = 0
    page = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/{table}",
            headers={**headers, "Range-Unit": "items", "Range": f"{start}-{start + PAGE_SIZE - 1}"},
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        page += 1
        print(f"    page {page}: +{len(batch)} rows ({len(rows)} total)")
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def _iso(val: Any) -> str:
    """Postgres timestamptz arrives as ISO string already; normalize None → ''."""
    return str(val) if val else ""


# ---------- shape adapters (Supabase rows → Airtable-shaped records) --------------

def fetch_locations() -> list[dict]:
    """→ records shaped for build_location_maps().
    'id' is the locations.id uuid — the same value reviews.location_id carries,
    so the Google linked-record emulation below lines up."""
    print("  Fetching locations...")
    return [
        {
            "id": str(row["id"]),
            "fields": {
                "Location_Name":   row.get("location_name") or "",
                "Yelp_URL":        row.get("yelp_url") or "",
                "TripAdvisor_URL": row.get("tripadvisor_url") or "",
            },
        }
        for row in fetch_all_rows("locations")
    ]


def fetch_reviews() -> list[dict]:
    """→ records shaped for normalize_review().
    Google rows get the Airtable linked-record emulation: "Location": [<uuid>]."""
    print("  Fetching reviews...")
    records = []
    for row in fetch_all_rows("reviews"):
        fields: dict[str, Any] = {
            "Review_ID":             row.get("review_id") or "",
            "Source_Platform":       row.get("source_platform") or "",
            "Place_ID":              row.get("place_id") or "",
            "Rating":                row.get("rating"),
            "Review_Text":           row.get("review_text") or "",
            "Review_Date":           _iso(row.get("review_date")),
            "Owner_Response":        row.get("owner_response") or "",
            "Owner_Response_Date":   _iso(row.get("owner_response_date")),
            "Review_URL":            row.get("review_url") or "",
            "Reviewer_Name":         row.get("reviewer_name") or "",
            "Reviewer_Review_Count": row.get("reviewer_review_count") or 0,
            "Is_Local_Guide":        bool(row.get("is_local_guide")),
            "Likes_Count":           row.get("likes_count") or 0,
        }
        if row.get("location_id"):
            fields["Location"] = [str(row["location_id"])]
        records.append({"id": str(row["id"]), "fields": fields})
    return records


# review_analysis DB column → Airtable-style field name used by the compute code
_ANALYSIS_COLMAP = {
    "review_id":                   "Review_ID",
    "sentiment":                   "Sentiment",
    "sentiment_score":             "Sentiment_Score",
    "urgency":                     "Urgency",
    "is_actionable":               "Is_Actionable",
    "primary_theme":               "Primary_Theme",
    "all_themes":                  "All_Themes",
    "food_mentioned":              "Food_Mentioned",
    "staff_mentioned":             "Staff_Mentioned",
    "staff_sentiment":             "Staff_Sentiment",
    "complaint_category":          "Complaint_Category",
    "praise_category":             "Praise_Category",
    "mentions_competitor":         "Mentions_Competitor",
    "repeat_customer":             "Repeat_Customer",
    "mentions_refund_or_recovery": "Mentions_Refund_Or_Recovery",
    "suggested_response_tone":     "Suggested_Response_Tone",
    "key_points_to_address":       "Key_Points_To_Address",
    "analysis_summary":            "Analysis_Summary",
    "analyzed_at":                 "Analyzed_At",
    "analysis_model":              "Analysis_Model",
}


def fetch_analysis() -> list[dict]:
    """→ records shaped for the analysis_map builder in build_v3.py."""
    print("  Fetching review analysis...")
    records = []
    for row in fetch_all_rows("review_analysis"):
        fields = {}
        for col, name in _ANALYSIS_COLMAP.items():
            val = row.get(col)
            if val is not None:
                fields[name] = val
        records.append({"id": str(row["id"]), "fields": fields})
    return records


def fetch_staff() -> list[dict]:
    """→ records shaped for staff_roster.build_people() (space-separated names)."""
    print("  Fetching staff roster...")
    return [
        {
            "id": str(row["id"]),
            "fields": {
                "First Name":            row.get("first_name") or "",
                "Last Name":             row.get("last_name") or "",
                "Full Name":             row.get("full_name") or "",
                "Aliases":               row.get("aliases") or "",
                "Locations Can Work At": row.get("locations_can_work_at") or "",
                "Department":            row.get("department") or "",
                "Employment Status":     row.get("employment_status") or "",
            },
        }
        for row in fetch_all_rows("staff_roster")
    ]


if __name__ == "__main__":
    # Smoke test: prints row counts for all four tables.
    for fn in (fetch_locations, fetch_reviews, fetch_analysis, fetch_staff):
        recs = fn()
        print(f"  {fn.__name__}: {len(recs)} records")
        if recs:
            print(f"    sample fields: {list(recs[0]['fields'].keys())}")
