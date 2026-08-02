#!/usr/bin/env python3
"""
GuestLens v3 — build script (Supabase-native client).

Same battle-tested compute pipeline as the TBC deployment, with the fetch
layer swapped from Airtable to Supabase (fetch_supabase.py) and all
client-specific values pulled from client_config.py.

Usage:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_SERVICE_KEY=eyJ... python build_v3.py
    → writes index.html (set OUTPUT_FILE=index_preview.html for a safe preview)
"""
from __future__ import annotations

import base64
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from build import (
    HERE,
    NEGATIVE_STARS,
    dedupe_reviews,
)
from build_compute import (
    build_location_maps,
    compute_analysis_breakdown_for_reviews,
    compute_period_snapshot,
    normalize_review,
)
from client_config import BRAND, EXCLUDED_LOCATIONS, SHORT_LOC
from fetch_supabase import fetch_analysis, fetch_locations, fetch_reviews, fetch_staff
import staff_roster

TEMPLATE_FILE = "template_v3.html"
# Defaults to the live file; set OUTPUT_FILE=index_preview.html to build a
# throwaway preview that never touches the deployed page.
OUTPUT_FILE   = os.environ.get("OUTPUT_FILE", "index.html")

# ---------- dish intelligence ---------------------------------------------------

GENERIC_FOOD_WORDS = {
    "food", "breakfast", "lunch", "brunch", "dinner", "meal", "meals", "menu",
    "everything", "dishes", "dish", "items", "drinks", "service", "n/a", "none",
}

def compute_dishes(
    reviews: list[dict], analysis_map: dict,
    min_mentions: int = 3, top: int = 15,
) -> list[dict]:
    """Aggregate Food_Mentioned across a review subset → ranked dish list."""
    stats: dict[str, dict] = {}
    for r in reviews:
        ana = analysis_map.get(r.get("id", ""), {})
        raw = (ana.get("Food_Mentioned") or "").strip()
        if not raw:
            continue
        sent = (ana.get("Sentiment") or "").strip()
        seen_in_review: set[str] = set()
        for name in raw.split(","):
            key = name.strip().lower().rstrip(".")
            if not key or key in GENERIC_FOOD_WORDS or len(key) > 40 or key in seen_in_review:
                continue
            seen_in_review.add(key)
            d = stats.setdefault(key, {"mentions": 0, "pos": 0, "neg": 0})
            d["mentions"] += 1
            if sent == "Positive":
                d["pos"] += 1
            elif sent in ("Negative", "Mixed"):
                d["neg"] += 1
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["mentions"], reverse=True)
    return [
        {"name": k.title(), **v}
        for k, v in ranked
        if v["mentions"] >= min_mentions
    ][:top]


# ---------- category splitting ---------------------------------------------------

def compute_split_breakdown(reviews: list[dict], analysis_map: dict, field: str) -> dict[str, int]:
    """Like compute_analysis_breakdown_for_reviews, but splits compound values
    ("Food, Service, Staff") so each part counts once. Keeps categories clean."""
    counts: dict[str, int] = {}
    for r in reviews:
        raw = (analysis_map.get(r.get("id", ""), {}).get(field) or "").strip()
        for part in raw.split(","):
            p = part.strip()
            if p and p != "None":
                counts[p] = counts.get(p, 0) + 1
    return counts


# ---------- deltas ---------------------------------------------------------------

def _window_stats(reviews: list[dict], analysis_map: dict) -> dict | None:
    if not reviews:
        return None
    stars = [r["stars"] for r in reviews if r.get("stars") is not None]
    responded = sum(1 for r in reviews if r.get("responseText"))
    sent = compute_analysis_breakdown_for_reviews(reviews, analysis_map, "Sentiment")
    total_sent = sum(sent.values())
    return {
        "count": len(reviews),
        "avgStars": (sum(stars) / len(stars)) if stars else None,
        "responseRate": responded / len(reviews),
        "positiveRate": (sent.get("Positive", 0) / total_sent * 100) if total_sent else None,
    }


def compute_deltas(
    all_reviews: list[dict], analysis_map: dict,
    ref_dt: datetime, days: int | None,
) -> dict | None:
    """Current window vs the immediately-preceding window of equal length."""
    if days is None:
        return None
    cur_start = ref_dt - timedelta(days=days)
    prev_start = ref_dt - timedelta(days=2 * days)
    cur  = [r for r in all_reviews if r.get("publishedAtDt") and r["publishedAtDt"] >= cur_start]
    prev = [r for r in all_reviews if r.get("publishedAtDt") and prev_start <= r["publishedAtDt"] < cur_start]

    def diff(c: dict | None, p: dict | None) -> dict | None:
        if not c or not p:
            return None
        out: dict[str, Any] = {"count": c["count"] - p["count"]}
        out["avgStars"] = round(c["avgStars"] - p["avgStars"], 2) \
            if c["avgStars"] is not None and p["avgStars"] is not None else None
        out["responseRate"] = round(c["responseRate"] - p["responseRate"], 3)
        out["positiveRate"] = round(c["positiveRate"] - p["positiveRate"], 1) \
            if c["positiveRate"] is not None and p["positiveRate"] is not None else None
        return out

    network = diff(_window_stats(cur, analysis_map), _window_stats(prev, analysis_map))

    by_location: dict[str, Any] = {}
    locs = {r["loc"] for r in cur} | {r["loc"] for r in prev}
    for loc in locs:
        d = diff(
            _window_stats([r for r in cur if r["loc"] == loc], analysis_map),
            _window_stats([r for r in prev if r["loc"] == loc], analysis_map),
        )
        if d:
            by_location[loc] = d

    if not network and not by_location:
        return None
    return {"network": network, "byLocation": by_location}


# ---------- per-location AI breakdowns -------------------------------------------

def compute_by_location_analysis(reviews: list[dict], analysis_map: dict) -> dict:
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        by_loc[r["loc"]].append(r)
    out: dict[str, Any] = {}
    for loc, revs in by_loc.items():
        sent = compute_analysis_breakdown_for_reviews(revs, analysis_map, "Sentiment")
        total = sum(sent.values())
        out[loc] = {
            "sentimentBreakdown":  sent,
            "urgencyBreakdown":    compute_analysis_breakdown_for_reviews(revs, analysis_map, "Urgency"),
            "themeBreakdown":      compute_analysis_breakdown_for_reviews(revs, analysis_map, "Primary_Theme"),
            "complaintCategories": compute_split_breakdown(revs, analysis_map, "Complaint_Category"),
            "praiseCategories":    compute_split_breakdown(revs, analysis_map, "Praise_Category"),
            "positiveRate":        round(sent.get("Positive", 0) / total * 100, 1) if total else None,
            "dishes":              compute_dishes(revs, analysis_map),
        }
    return out


# ---------- action center ---------------------------------------------------------

URGENCY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "None": 4}

def build_action_queue(
    all_reviews: list[dict], analysis_map: dict,
    ref_dt: datetime, limit: int = 40,
) -> list[dict]:
    items = []
    for r in all_reviews:
        stars = r.get("stars")
        if stars is None or stars > NEGATIVE_STARS or r.get("responseText"):
            continue
        ana = analysis_map.get(r.get("id", ""), {})
        key_points = [
            ln.strip().lstrip("-•").strip()
            for ln in (ana.get("Key_Points_To_Address") or "").splitlines()
            if ln.strip()
        ]
        age = None
        if r.get("publishedAtDt"):
            age = round((ref_dt - r["publishedAtDt"]).total_seconds() / 86400, 1)
        items.append({
            "date":     r.get("publishedAt", "")[:10],
            "loc":      r.get("loc", ""),
            "source":   r.get("source", ""),
            "rating":   stars,
            "text":     (r.get("text") or "")[:900],
            "reviewer": (r.get("reviewer") or {}).get("name") or "Guest",
            "url":      r.get("url") or "",
            "urgency":  (ana.get("Urgency") or "").strip() or None,
            "tone":     (ana.get("Suggested_Response_Tone") or "").strip() or None,
            "keyPoints": key_points or None,
            "summary":  (ana.get("Analysis_Summary") or "").strip() or None,
            "ageDays":  age,
        })
    # urgency first (Critical → High → Medium → …), newest first within each level
    items.sort(key=lambda a: a["date"], reverse=True)
    items.sort(key=lambda a: URGENCY_RANK.get(a["urgency"], 2))
    return items[:limit]


# ---------- recent reviews (extended) ----------------------------------------------

def build_recent_reviews_v3(
    all_reviews: list[dict], analysis_map: dict, limit: int = 400,
) -> list[dict]:
    sorted_revs = sorted(all_reviews, key=lambda r: r.get("publishedAt", ""), reverse=True)[:limit]
    result = []
    for r in sorted_revs:
        ana = analysis_map.get(r.get("id", ""), {})
        raw_staff = (ana.get("Staff_Mentioned") or "").strip()
        staff = ", ".join(n.strip() for n in raw_staff.split(",") if n.strip()) if raw_staff else ""
        result.append({
            "reviewer":      (r.get("reviewer") or {}).get("name") or "Guest",
            "rating":        r.get("stars"),
            "text":          (r.get("text") or "")[:700],
            "date":          r.get("publishedAt", "")[:10],
            "loc":           r.get("loc", ""),
            "source":        r.get("source", ""),
            "sentiment":     (ana.get("Sentiment") or "").strip(),
            "urgency":       (ana.get("Urgency") or "").strip(),
            "summary":       (ana.get("Analysis_Summary") or "").strip(),
            "staffMentioned": staff,
            "theme":         (ana.get("Primary_Theme") or "").strip(),
            "responded":     bool(r.get("responseText")),
            "ownerResponse": (r.get("responseText") or "")[:500],
            "url":           r.get("url") or "",
        })
    return result


# ---------- brand ------------------------------------------------------------------

def brand_payload() -> dict:
    payload = {k: v for k, v in BRAND.items() if k != "logoPath"}
    logo = HERE / BRAND["logoPath"]
    if logo.exists():
        b64 = base64.b64encode(logo.read_bytes()).decode()
        payload["logo"] = f"data:image/png;base64,{b64}"
    else:
        print(f"  WARNING: logo not found at {logo} — building without logo")
        payload["logo"] = ""
    return payload


# ---------- main -------------------------------------------------------------------

PERIOD_CONFIGS = [
    ("7d",   7,    "Last 7 Days",   True),
    ("30d",  30,   "Last 30 Days",  True),
    ("90d",  90,   "Last 90 Days",  False),
    ("180d", 180,  "Last 6 Months", False),
    ("365d", 365,  "Last Year",     False),
    ("all",  None, "All Time",      False),
]


def main() -> None:
    print("Fetching locations from Supabase...")
    location_records = fetch_locations()
    rec_map, yelp_map, ta_map = build_location_maps(location_records)

    print("Fetching review analysis from Supabase...")
    analysis_records = fetch_analysis()
    analysis_map = {
        rec["fields"]["Review_ID"]: rec["fields"]
        for rec in analysis_records
        if "Review_ID" in rec.get("fields", {})
    }
    print(f"  {len(analysis_map)} analysis records keyed")

    print("Fetching reviews from Supabase...")
    review_records = fetch_reviews()

    pre_norm = []
    for rec in review_records:
        norm = normalize_review(rec, rec_map, yelp_map, ta_map, analysis_map)
        if norm and norm["loc"] not in EXCLUDED_LOCATIONS:
            pre_norm.append(norm)
    all_reviews, _ = dedupe_reviews(pre_norm)
    print(f"  {len(all_reviews)} reviews after normalize + dedup")

    last_dt = max(
        (r["publishedAtDt"] for r in all_reviews if r.get("publishedAtDt")),
        default=datetime.now(timezone.utc),
    )

    # ---- staff roster (name → location ground truth). Fail-safe: if the table
    # can't be read or is empty, staff_people stays empty and each snapshot keeps
    # compute_period_snapshot's original leaderboard (no crash, graceful fallback).
    staff_people: list[dict] = []
    staff_index: dict = {}
    try:
        print("Fetching staff roster from Supabase...")
        staff_records = fetch_staff()
        staff_people = staff_roster.build_people(staff_records)
        staff_index = staff_roster.build_index(staff_people)
        print(f"  {len(staff_people)} staff, {len(staff_index)} name/alias keys")
    except Exception as e:  # noqa: BLE001 — roster is an enhancement, never a hard dep
        print(f"  WARNING: roster unavailable ({e}); Staff Stars falls back to review-location attribution")

    # ---- period snapshots + v3 enrichment
    print("Computing period snapshots...")
    periods: dict[str, Any] = {}
    for key, days, label, inc_brief in PERIOD_CONFIGS:
        if days is None:
            subset = all_reviews
        else:
            cutoff = last_dt - timedelta(days=days)
            subset = [r for r in all_reviews if r.get("publishedAtDt") and r["publishedAtDt"] >= cutoff]
        snap = compute_period_snapshot(subset, analysis_map, label=label, include_brief=inc_brief)
        # split compound singleSelect values ("Food, Service, Staff") into parts
        snap["complaintCategories"] = compute_split_breakdown(subset, analysis_map, "Complaint_Category")
        snap["praiseCategories"]    = compute_split_breakdown(subset, analysis_map, "Praise_Category")
        snap["deltas"] = compute_deltas(all_reviews, analysis_map, last_dt, days)
        snap["dishes"] = compute_dishes(subset, analysis_map)
        snap["byLocationAnalysis"] = compute_by_location_analysis(subset, analysis_map)
        # roster-resolved Staff Stars — overwrites compute_period_snapshot's
        # review-location leaderboard with location-correct, verified attribution
        # (+ needs-review bucket + per-store verified mention totals). Skipped when
        # the roster is unavailable, leaving the original leaderboard in place.
        if staff_people:
            snap.update(staff_roster.compute_staff_stars(subset, analysis_map, staff_people, staff_index))
        periods[key] = snap
        vloc = snap.get("staffMentionsByLoc", {})
        print(f"  {key}: {len(subset)} reviews · dishes={len(snap['dishes'])} · "
              f"staff verified/store={vloc or 'n/a'}")

    # ---- network (all-time) for header/footer
    all_snap = periods["all"]

    DATA: dict[str, Any] = {
        "product":       BRAND["name"],
        "client":        BRAND["client"],
        "brand":         brand_payload(),
        "sources":       sorted({r["source"] for r in all_reviews}),
        "lastReviewAt":  last_dt.isoformat(),
        "generatedAt":   datetime.now(timezone.utc).isoformat(),
        "locationsOrder": all_snap.get("locationsOrder", []),
        "network":       all_snap.get("network", {}),
        "periods":       periods,
        "defaultPeriod": "30d",
        "shortLoc":      SHORT_LOC,
        "recentReviews": build_recent_reviews_v3(all_reviews, analysis_map, 400),
        "actionQueue":   build_action_queue(all_reviews, analysis_map, last_dt),
        "recentNegatives": [],   # superseded by actionQueue in v3
    }

    template = (HERE / TEMPLATE_FILE).read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/", json.dumps(DATA, ensure_ascii=False))
    out = HERE / OUTPUT_FILE
    out.write_text(html, encoding="utf-8")

    print(f"\n{'=' * 54}")
    print(f"Wrote {out.name}  ({out.stat().st_size / 1024:.0f} KB)")
    print(f"Reviews : {DATA['network'].get('n')}  across {len(DATA['locationsOrder'])} locations")
    print(f"Action queue : {len(DATA['actionQueue'])} unanswered negatives")
    print(f"Dishes (all-time) : {[d['name'] for d in periods['all']['dishes'][:8]]}")
    print(f"{'=' * 54}")


if __name__ == "__main__":
    main()
