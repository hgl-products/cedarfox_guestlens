#!/usr/bin/env python3
"""
GuestLens — pure compute module (backend-agnostic).

This is build_airtable.py from the TBC deployment with the Airtable config,
fetch layer, and main() stripped out. Only the battle-tested computation
functions remain; they consume Airtable-SHAPED records
({"id": ..., "fields": {...}}) regardless of which backend produced them.
For Supabase clients, fetch_supabase.py produces those records.

Never add network code here — fetch layers live in their own modules.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# Import all computation logic from the original build.py (unchanged)
from build import (
    HERE,
    NEGATIVE_STARS,
    RATING_GREEN,
    RATING_YELLOW,
    RECENT_BAD_DAYS,
    RECENT_BAD_PRIMARY,
    RESPONSE_GREEN,
    RESPONSE_YELLOW,
    aggregate_metrics,
    build_ai_brief,
    dedupe_reviews,
    derive_source_findings,
    parse_iso,
    text_hash,
    week_start,
)


# ---------- location maps -----------------------------------------------------

def build_location_maps(
    location_records: list[dict],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    Return three lookup dicts from the Locations table:
      record_id_to_label  — Airtable record ID  → Location_Name  (Google reviews)
      yelp_alias_to_label — Yelp alias slug      → Location_Name  (Yelp reviews)
      ta_id_to_label      — TripAdvisor numeric  → Location_Name  (TA reviews)
    """
    record_id_to_label: dict[str, str] = {}
    yelp_alias_to_label: dict[str, str] = {}
    ta_id_to_label: dict[str, str] = {}

    for rec in location_records:
        f = rec.get("fields", {})
        label = f.get("Location_Name", "").strip()
        if not label:
            continue

        record_id_to_label[rec["id"]] = label

        # Yelp URL  →  https://www.yelp.com/biz/<alias>
        yelp_url = f.get("Yelp_URL") or ""
        m = re.search(r"/biz/([^/?#]+)", yelp_url)
        if m:
            yelp_alias_to_label[m.group(1).rstrip("/")] = label

        # TripAdvisor URL  →  ...Restaurant_Review-gXXXX-dNNNNNN-...
        ta_url = f.get("TripAdvisor_URL") or ""
        m = re.search(r"-d(\d+)-", ta_url)
        if m:
            ta_id_to_label[m.group(1)] = label

    return record_id_to_label, yelp_alias_to_label, ta_id_to_label


# ---------- normalization ------------------------------------------------------

def normalize_review(
    rec: dict,
    record_id_to_label: dict[str, str],
    yelp_alias_to_label: dict[str, str],
    ta_id_to_label: dict[str, str],
    analysis_map: dict[str, dict],
) -> dict | None:
    """
    Convert one Airtable Individual_Reviews record to the unified review schema
    used by build.py's downstream computation functions.
    Returns None if the review cannot be placed in a known location or has no date.
    """
    f = rec.get("fields", {})

    platform_raw = (f.get("Source_Platform") or "").strip().lower()
    source = {
        "google":      "Google",
        "yelp":        "Yelp",
        "tripadvisor": "Tripadvisor",
        "ubereats":    "Uber Eats",
        "doordash":    "DoorDash",
        "myrewards":   "My Rewards",
    }.get(platform_raw, platform_raw.capitalize())

    # Resolve location label from platform-specific ID
    if platform_raw == "google":
        linked_ids = f.get("Location") or []
        loc = record_id_to_label.get(linked_ids[0]) if linked_ids else None
    elif platform_raw == "yelp":
        loc = yelp_alias_to_label.get((f.get("Place_ID") or "").strip())
    elif platform_raw == "tripadvisor":
        loc = ta_id_to_label.get((f.get("Place_ID") or "").strip())
    else:
        loc = None   # manual-import platforms (Uber Eats, DoorDash, My Rewards)
                     # will be supported once location mapping is configured

    if not loc:
        return None  # skip reviews we cannot assign to a location

    published = parse_iso(f.get("Review_Date") or "")
    if not published:
        return None  # skip reviews without a parseable date

    review_id = (f.get("Review_ID") or "").strip()
    analysis = analysis_map.get(review_id, {})

    return {
        "source":       source,
        "loc":          loc,
        "stars":        f.get("Rating"),
        "text":         f.get("Review_Text") or "",
        "title":        "",
        "publishedAt":  published.isoformat(),
        "publishedAtDt": published,
        "responseText": f.get("Owner_Response") or "",
        "responseAt":   parse_iso(f.get("Owner_Response_Date") or ""),
        "subratings":   {},   # overridden below from analysis data
        "context":      {},
        "tripType":     None,
        "reviewer": {
            "name":         f.get("Reviewer_Name") or "",
            "experience":   f.get("Reviewer_Review_Count") or 0,
            "isLocalGuide": bool(f.get("Is_Local_Guide")),
        },
        "url":                  f.get("Review_URL") or "",
        "id":                   review_id,
        "language":             None,
        "platformScore":        None,
        "platformReviewsCount": None,
        # internal — used only for subrating computation, not passed to DATA
        "_primary_theme": analysis.get("Primary_Theme"),
    }


# ---------- analysis-based subratings -----------------------------------------

def compute_subratings(reviews: list[dict]) -> dict[str, float]:
    """
    Average star rating per Primary_Theme for the given review set.
    Replaces the raw per-review subrating fields that TripAdvisor used to provide.
    """
    theme_stars: dict[str, list[float]] = defaultdict(list)
    for r in reviews:
        theme = r.get("_primary_theme")
        stars = r.get("stars")
        if theme and stars is not None:
            theme_stars[theme].append(float(stars))
    return {
        theme: round(statistics.mean(vals), 2)
        for theme in ["Food", "Service", "Atmosphere", "Value"]
        for vals in [theme_stars[theme]]
        if vals
    }


# ---------- v2 analytics helpers ----------------------------------------------

def compute_staff_leaderboard(all_reviews: list[dict], analysis_map: dict) -> list[dict]:
    """Parse Staff_Mentioned (comma-separated names) → top-25 [{name, mentions, loc}]."""
    name_counts: Counter = Counter()
    name_loc: dict[str, str] = {}
    for r in all_reviews:
        rid = r.get("id", "")
        raw = (analysis_map.get(rid, {}).get("Staff_Mentioned") or "").strip()
        if not raw:
            continue
        for name in [n.strip() for n in raw.split(",") if n.strip()]:
            name_counts[name] += 1
            if name not in name_loc:
                name_loc[name] = r.get("loc", "")
    return [
        {"name": name, "mentions": count, "loc": name_loc.get(name, "")}
        for name, count in name_counts.most_common(25)
    ]


def compute_analysis_breakdown(analysis_map: dict, field: str) -> dict[str, int]:
    """Count occurrences of a singleSelect field across all analysis records."""
    counts: Counter = Counter()
    for ana in analysis_map.values():
        val = (ana.get(field) or "").strip()
        if val:
            counts[val] += 1
    return dict(counts)


def compute_analysis_breakdown_for_reviews(
    reviews: list[dict], analysis_map: dict, field: str
) -> dict[str, int]:
    """Count a singleSelect field only for the given review subset."""
    counts: Counter = Counter()
    for r in reviews:
        val = (analysis_map.get(r.get("id", ""), {}).get(field) or "").strip()
        if val:
            counts[val] += 1
    return dict(counts)


def compute_star_distribution(reviews: list[dict]) -> dict[str, int]:
    """Count reviews per 1-5 star rating."""
    dist: Counter = Counter()
    for r in reviews:
        stars = r.get("stars")
        if stars is not None:
            dist[str(int(stars))] += 1
    return {str(s): dist.get(str(s), 0) for s in range(1, 6)}


def build_recent_reviews(
    all_reviews: list[dict], analysis_map: dict, limit: int = 150
) -> list[dict]:
    """Top-N most-recent reviews enriched with AI summary, sentiment, staff names."""
    sorted_revs = sorted(
        all_reviews,
        key=lambda r: r.get("publishedAt", ""),
        reverse=True,
    )[:limit]
    result = []
    for r in sorted_revs:
        rid = r.get("id", "")
        ana = analysis_map.get(rid, {})
        raw_staff = (ana.get("Staff_Mentioned") or "").strip()
        staff_names = [n.strip() for n in raw_staff.split(",") if n.strip()] if raw_staff else []
        reviewer = r.get("reviewer") or {}
        result.append({
            "reviewer":      reviewer.get("name") or "Guest",
            "rating":        r.get("stars"),
            "text":          (r.get("text") or "")[:600],
            "date":          r.get("publishedAt", "")[:10],
            "loc":           r.get("loc", ""),
            "source":        r.get("source", ""),
            "sentiment":     (ana.get("Sentiment") or "").strip(),
            "urgency":       (ana.get("Urgency") or "").strip(),
            "summary":       (ana.get("Analysis_Summary") or "").strip(),
            "staffMentioned": ", ".join(staff_names),
            "theme":         (ana.get("Primary_Theme") or "").strip(),
            "responded":     bool(r.get("responseText")),
        })
    return result


# ---------- period snapshots --------------------------------------------------

def compute_period_snapshot(
    reviews_subset: list[dict],
    analysis_map: dict,
    label: str | None = None,
    include_brief: bool = False,
) -> dict[str, Any]:
    """
    Compute all dashboard metrics for a date-filtered subset of reviews.
    Returns a dict with the same shape as the top-level DATA fields so the
    template can swap between periods without changing any rendering logic.
    """
    if not reviews_subset:
        return {"empty": True, "reviewCount": 0, "brief": None}

    by_loc: dict[str, list[dict]] = defaultdict(list)
    for r in reviews_subset:
        by_loc[r["loc"]].append(r)

    locs = sorted(by_loc.keys())
    per_loc_m: dict[str, Any] = {}
    recent_negs: list[dict] = []

    for loc in locs:
        loc_revs = sorted(by_loc[loc], key=lambda x: x["publishedAtDt"], reverse=True)
        m = aggregate_metrics(loc_revs)
        m["subratings"] = compute_subratings(loc_revs)
        per_loc_m[loc] = m
        for sr in [serialize(rv) for rv in loc_revs]:
            if (sr["stars"] or 5) <= NEGATIVE_STARS and not sr["responseText"]:
                recent_negs.append(sr)

    network = aggregate_metrics(reviews_subset)
    network["subratings"] = compute_subratings(reviews_subset)
    src_findings = derive_source_findings(network)

    sentiment_bd = compute_analysis_breakdown_for_reviews(reviews_subset, analysis_map, "Sentiment")
    total_sent   = sum(sentiment_bd.values())
    pos_rate     = round(sentiment_bd.get("Positive", 0) / total_sent * 100, 1) if total_sent else 0.0
    network["positiveRate"] = pos_rate

    brief: str | None = None
    if include_brief:
        raw = build_ai_brief(per_loc_m, network, locs, recent_negs, src_findings)
        # build_ai_brief returns list[{"kind":..., "text":...}]
        if isinstance(raw, list):
            raw_str = "\n\n".join(
                item["text"] for item in raw
                if isinstance(item, dict) and item.get("text")
            )
        else:
            raw_str = str(raw)
        brief = (f"📅  {label}\n\n" + raw_str) if label else raw_str

    return {
        "locationsOrder":     locs,
        "network":            network,
        "byLocationMetrics":  per_loc_m,
        "sentimentBreakdown": sentiment_bd,
        "urgencyBreakdown":   compute_analysis_breakdown_for_reviews(reviews_subset, analysis_map, "Urgency"),
        "themeBreakdown":     compute_analysis_breakdown_for_reviews(reviews_subset, analysis_map, "Primary_Theme"),
        "complaintCategories": compute_analysis_breakdown_for_reviews(reviews_subset, analysis_map, "Complaint_Category"),
        "praiseCategories":   compute_analysis_breakdown_for_reviews(reviews_subset, analysis_map, "Praise_Category"),
        "staffLeaderboard":   compute_staff_leaderboard(reviews_subset, analysis_map),
        "starDistribution":   compute_star_distribution(reviews_subset),
        "positiveRate":       pos_rate,
        "reviewCount":        len(reviews_subset),
        "brief":              brief,
        "empty":              False,
    }


# ---------- serializer --------------------------------------------------------

def serialize(r: dict) -> dict:
    """Strip internal fields and make a review JSON-serializable."""
    return {
        "source":       r["source"],
        "loc":          r["loc"],
        "stars":        r["stars"],
        "text":         r["text"],
        "title":        r.get("title", ""),
        "publishedAt":  r["publishedAt"],
        "responseText": r["responseText"],
        "responseAt":   r["responseAt"].isoformat() if r.get("responseAt") else None,
        "subratings":   r.get("subratings") or {},
        "context":      r.get("context") or {},
        "tripType":     r.get("tripType"),
        "reviewer":     r.get("reviewer") or {},
        "url":          r.get("url"),
        "id":           r.get("id"),
        "language":     r.get("language"),
        "platformScore":        r.get("platformScore"),
        "platformReviewsCount": r.get("platformReviewsCount"),
    }


