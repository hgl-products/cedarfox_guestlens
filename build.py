#!/usr/bin/env python3
"""
Review Command Center — multi-source build script.

Auto-discovers Apify dataset JSON files in the same folder, detects the
source from the filename (Google Maps Reviews Scraper, TripAdvisor, etc.),
normalizes each into a unified review schema, then emits a single
self-contained HTML dashboard for Dimitri.

Adding a new source = write one normalizer function and add an entry to
SOURCE_REGISTRY. Everything downstream (metrics, AI brief, dashboard) is
source-agnostic.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).parent

# ---------- thresholds (Dimitri's decision logic) -------------------------
RATING_GREEN = 4.5
RATING_YELLOW = 4.0
RESPONSE_GREEN = 0.80
RESPONSE_YELLOW = 0.50
NEGATIVE_STARS = 3
RECENT_BAD_DAYS = 14   # surface bad reviews from this window
RECENT_BAD_PRIMARY = 7 # tighter window for "this week"

# ---------- location resolver --------------------------------------------
# Switched from prefix matching to street-number matching so we tolerate
# spelling variants ("S. Tamiami Trail" vs "S Tamiami Trl",
# "North Orange Avenue" vs "N Orange Ave") and ZIP+4 differences.
LOCATIONS: dict[str, dict[str, Any]] = {
    "8491": {"label": "Cooper Creek (UTC)", "city": "Bradenton"},
    "4832": {"label": "S Tamiami (Sarasota)", "city": "Sarasota"},
    "7246": {"label": "55th Ave (Bradenton)", "city": "Bradenton"},
    "411":  {"label": "Downtown Sarasota", "city": "Sarasota"},
}


def resolve_location(addr: str | None) -> str | None:
    if not addr:
        return None
    m = re.match(r"\s*(\d+)\b", addr)
    if not m:
        return None
    return LOCATIONS.get(m.group(1), {}).get("label")


# ---------- helpers --------------------------------------------------------

def parse_iso(d: str | None) -> datetime | None:
    if not d:
        return None
    # accept ISO with Z, ISO without TZ, or YYYY-MM-DD (TripAdvisor)
    try:
        if len(d) == 10:  # YYYY-MM-DD
            return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
        # Force-UTC any naive datetime so cross-source comparisons don't blow up
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def parse_loose_date(s: Any) -> datetime | None:
    """Parse the various ad-hoc formats: ISO, YYYY-MM-DD, MM/DD/YY, list-wrapped."""
    if not s:
        return None
    if isinstance(s, list):
        s = s[0] if s else None
        if not s:
            return None
    if not isinstance(s, str):
        return None
    dt = parse_iso(s)
    if dt:
        return dt
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def text_hash(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(("|" + str(p or "")).encode("utf-8"))
    return h.hexdigest()[:16]


def week_start(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def status_for_rating(avg: float) -> str:
    if avg >= RATING_GREEN: return "GREEN"
    if avg >= RATING_YELLOW: return "YELLOW"
    return "RED"


def status_for_response(rate: float) -> str:
    if rate >= RESPONSE_GREEN: return "GREEN"
    if rate >= RESPONSE_YELLOW: return "YELLOW"
    return "RED"


# ---------- per-source normalizers ----------------------------------------
# Every normalizer returns a list of dicts in the unified schema.

def normalize_google(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in raw:
        loc = resolve_location(r.get("address"))
        if not loc:
            continue
        published = parse_iso(r.get("publishedAtDate"))
        if not published:
            continue
        out.append({
            "source": "Google",
            "loc": loc,
            "stars": r.get("stars"),
            "text": r.get("text") or "",
            "publishedAt": published.isoformat(),
            "publishedAtDt": published,
            "responseText": r.get("responseFromOwnerText") or "",
            "responseAt": parse_iso(r.get("responseFromOwnerDate")),
            "subratings": r.get("reviewDetailedRating") or {},
            "context": r.get("reviewContext") or {},
            "tripType": (r.get("reviewContext") or {}).get("Meal type"),
            "reviewer": {
                "name": r.get("name"),
                "experience": r.get("reviewerNumberOfReviews") or 0,
                "isLocalGuide": bool(r.get("isLocalGuide")),
            },
            "url": r.get("url"),
            "id": r.get("reviewId"),
            "language": r.get("originalLanguage"),
            "platformScore": r.get("totalScore"),
            "platformReviewsCount": r.get("reviewsCount"),
        })
    return out


def normalize_tripadvisor(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in raw:
        place = r.get("placeInfo") or {}
        loc = resolve_location(place.get("address"))
        if not loc:
            continue
        published = parse_iso(r.get("publishedDate"))
        if not published:
            continue
        # subratings is a list -> dict
        subs = {s.get("name"): s.get("value") for s in (r.get("subratings") or []) if s.get("name")}

        # owner response — TripAdvisor returns an object when present
        owner = r.get("ownerResponse") or {}
        resp_text = owner.get("text") if isinstance(owner, dict) else (owner or "")
        resp_date = parse_iso(owner.get("publishedDate")) if isinstance(owner, dict) else None

        user = r.get("user") or {}
        contributions = (user.get("contributions") or {}).get("totalContributions", 0)

        out.append({
            "source": "TripAdvisor",
            "loc": loc,
            "stars": r.get("rating"),
            "text": r.get("text") or "",
            "title": r.get("title") or "",
            "publishedAt": published.isoformat(),
            "publishedAtDt": published,
            "responseText": resp_text or "",
            "responseAt": resp_date,
            "subratings": subs,
            "context": {"Trip type": r.get("tripType")} if r.get("tripType") else {},
            "tripType": r.get("tripType"),
            "reviewer": {
                "name": user.get("name") or user.get("username"),
                "experience": contributions,
                "isLocalGuide": False,
            },
            "url": r.get("url"),
            "id": r.get("id"),
            "language": r.get("lang"),
            "platformScore": place.get("rating"),
            "platformReviewsCount": place.get("numberOfReviews"),
        })
    return out


def normalize_uber_eats(raw: list[dict]) -> list[dict]:
    """Uber Eats actor returns one record per store, with nested storeReviews + featuredReviews."""
    out: list[dict] = []
    seen_uuid: set[str] = set()
    for store in raw:
        loc = resolve_location((store.get("location") or {}).get("address"))
        if not loc:
            continue
        platform_score = (store.get("rating") or {}).get("ratingValue")
        platform_count_raw = (store.get("rating") or {}).get("reviewCount")
        # reviewCount comes as "500+" / "900+" — strip non-digits
        platform_count = None
        if isinstance(platform_count_raw, str):
            m = re.search(r"\d+", platform_count_raw)
            if m:
                platform_count = int(m.group()) if "+" not in platform_count_raw else f"{m.group()}+"
            else:
                platform_count = platform_count_raw
        elif isinstance(platform_count_raw, (int, float)):
            platform_count = platform_count_raw

        # featuredReviews and storeReviews can overlap by contentUUID — dedup local to this store
        all_reviews = (store.get("storeReviews") or []) + (store.get("featuredReviews") or [])
        for r in all_reviews:
            uuid = r.get("contentUUID")
            if uuid and uuid in seen_uuid:
                continue
            if uuid:
                seen_uuid.add(uuid)
            published = parse_iso(r.get("createdAt")) or parse_loose_date(r.get("formattedDate"))
            if not published:
                continue
            out.append({
                "source": "Uber Eats",
                "loc": loc,
                "stars": r.get("rating"),
                "text": r.get("text") or "",
                "publishedAt": published.isoformat(),
                "publishedAtDt": published,
                "responseText": "",  # Uber Eats has no owner-response surface
                "responseAt": None,
                "subratings": {},
                "context": {},
                "tripType": None,
                "reviewer": {
                    "name": r.get("eaterName"),
                    "experience": 0,
                    "isLocalGuide": False,
                },
                "url": store.get("url"),
                "id": uuid or text_hash(r.get("text"), r.get("createdAt"), loc),
                "language": None,
                "platformScore": platform_score,
                "platformReviewsCount": platform_count,
            })
    return out


def normalize_aggregator(raw: list[dict]) -> list[dict]:
    """
    Restaurant Review Aggregator actor — yields records from multiple providers.
    We treat this as a backstop: only records the dedicated source files missed.
    Each record carries `provider` ('google-maps', 'uber-eats', etc) so we map back to a source.
    """
    PROVIDER_MAP = {
        "google-maps": "Google",
        "google": "Google",
        "uber-eats": "Uber Eats",
        "ubereats": "Uber Eats",
        "tripadvisor": "TripAdvisor",
        "yelp": "Yelp",
        "doordash": "DoorDash",
    }
    out: list[dict] = []
    for r in raw:
        prov = (r.get("provider") or "").lower()
        source = PROVIDER_MAP.get(prov)
        if not source:
            continue
        loc = resolve_location(r.get("placeAddress"))
        if not loc:
            continue
        published = parse_loose_date(r.get("reviewDate"))
        if not published:
            continue
        rating = r.get("reviewRating")
        # Skip records with no rating — they break network metrics and we'd rather omit
        if rating is None:
            continue
        ext_id = r.get("reviewId") or text_hash(
            r.get("reviewText"), r.get("reviewDate"), loc
        )
        out.append({
            "source": source,
            "loc": loc,
            "stars": rating,
            "text": r.get("reviewText") or "",
            "title": r.get("reviewTitle") or "",
            "publishedAt": published.isoformat(),
            "publishedAtDt": published,
            "responseText": "",
            "responseAt": None,
            "subratings": {},
            "context": {},
            "tripType": None,
            "reviewer": {"name": r.get("authorName"), "experience": 0, "isLocalGuide": False},
            "url": r.get("reviewUrl") or r.get("placeUrl"),
            "id": ext_id,
            "language": None,
            "platformScore": None,
            "platformReviewsCount": None,
            "_fromAggregator": True,
        })
    return out


# Registry — order matters. Dedicated platform files are processed first; the
# aggregator is processed last so its records only fill gaps.
SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "Google": {
        "filename_match": re.compile(r"Google-Maps-Reviews-Scraper", re.I),
        "normalizer": normalize_google,
        "priority": 1,
    },
    "TripAdvisor": {
        "filename_match": re.compile(r"tripadvisor", re.I),
        "normalizer": normalize_tripadvisor,
        "priority": 1,
    },
    "Uber Eats": {
        "filename_match": re.compile(r"uber-eats", re.I),
        "normalizer": normalize_uber_eats,
        "priority": 1,
    },
    "Aggregator": {
        # name is internal — actual source on each record comes from the `provider` field
        "filename_match": re.compile(r"restaurant-review-aggregator", re.I),
        "normalizer": normalize_aggregator,
        "priority": 9,
    },
}


def discover_sources() -> list[tuple[str, str, list[dict]]]:
    """
    Return [(source_name, filename, raw_list), ...] for every dataset_*.json file.
    Sorted by source priority so dedicated-platform files run before aggregators.
    """
    found: list[tuple[str, str, list[dict], int]] = []
    for path in sorted(glob.glob(str(HERE / "dataset_*.json"))):
        name = os.path.basename(path)
        matched = None
        for src, cfg in SOURCE_REGISTRY.items():
            if cfg["filename_match"].search(name):
                matched = (src, cfg.get("priority", 5))
                break
        if not matched:
            print(f"  [skip] {name}  (no source matcher)")
            continue
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        found.append((matched[0], name, raw, matched[1]))
        print(f"  [load] {name}  source={matched[0]}  priority={matched[1]}  records={len(raw)}")
    found.sort(key=lambda x: x[3])  # priority asc — dedicated first
    return [(s, n, r) for s, n, r, _ in found]


def dedupe_reviews(all_norm: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """
    Global dedup. Key strategy:
      1. (source, id)  if id is non-empty
      2. (source, loc, fingerprint(text[:200], date_day))  for records without a stable id

    Records seen earlier (higher-priority sources) win.
    """
    keep: list[dict] = []
    seen: set[tuple] = set()
    stats: dict[str, int] = defaultdict(int)
    for r in all_norm:
        src = r["source"]
        rid = r.get("id")
        date_day = r["publishedAtDt"].strftime("%Y-%m-%d") if r.get("publishedAtDt") else ""
        text_sig = (r.get("text") or "").strip()[:200].lower()

        # primary key
        primary = (src, "ID:" + str(rid)) if rid else None
        # fingerprint key — used as alias to catch duplicates that don't share an external id
        fp = (src, "FP:" + text_hash(r["loc"], date_day, text_sig))

        if primary and primary in seen:
            stats[f"{src}:dup-by-id"] += 1
            continue
        if fp in seen:
            stats[f"{src}:dup-by-fingerprint"] += 1
            continue
        if primary:
            seen.add(primary)
        seen.add(fp)
        keep.append(r)
        stats[f"{src}:kept"] += 1
    return keep, dict(stats)


# ---------- metrics --------------------------------------------------------

def aggregate_metrics(reviews: list[dict]) -> dict[str, Any]:
    n = len(reviews)
    if not n:
        return {"n": 0}
    avg_stars = statistics.mean(r["stars"] for r in reviews if r.get("stars"))
    with_resp = sum(1 for r in reviews if r["responseText"])
    response_rate = with_resp / n

    # response lag (days) — only when both dates present
    lags = []
    for r in reviews:
        if r["responseText"] and r.get("publishedAtDt") and r.get("responseAt"):
            lags.append((r["responseAt"] - r["publishedAtDt"]).total_seconds() / 86400.0)
    median_response_lag = statistics.median(lags) if lags else None

    negatives = [r for r in reviews if (r["stars"] or 0) <= NEGATIVE_STARS]
    unanswered_negatives = [r for r in negatives if not r["responseText"]]

    # subratings — Google: Food/Service/Atmosphere; TripAdvisor: Value/Service/Food (+ atm sometimes)
    sub_aggregates: dict[str, list[float]] = defaultdict(list)
    for r in reviews:
        for k, v in (r.get("subratings") or {}).items():
            if isinstance(v, (int, float)):
                sub_aggregates[k].append(v)
    sub_avg = {k: round(statistics.mean(v), 2) for k, v in sub_aggregates.items() if v}

    stars_dist = Counter(r["stars"] for r in reviews if r.get("stars"))

    # source breakdown
    source_counts = Counter(r["source"] for r in reviews)
    source_resp = defaultdict(lambda: [0, 0])  # [responded, total]
    for r in reviews:
        source_resp[r["source"]][1] += 1
        if r["responseText"]:
            source_resp[r["source"]][0] += 1
    source_breakdown = {
        s: {
            "n": source_counts[s],
            "responded": source_resp[s][0],
            "respRate": source_resp[s][0] / source_resp[s][1] if source_resp[s][1] else 0,
            "avgStars": round(statistics.mean(r["stars"] for r in reviews if r["source"] == s and r.get("stars")), 2)
                if any(r["source"] == s and r.get("stars") for r in reviews) else None,
        }
        for s in source_counts
    }

    # weekly buckets
    weekly_counts: dict[str, int] = defaultdict(int)
    weekly_stars: dict[str, list[float]] = defaultdict(list)
    weekly_resp: dict[str, list[int]] = defaultdict(list)
    weekly_source: dict[str, Counter] = defaultdict(Counter)
    for r in reviews:
        if not r.get("publishedAtDt"):
            continue
        w = week_start(r["publishedAtDt"]).strftime("%Y-%m-%d")
        weekly_counts[w] += 1
        if r.get("stars"):
            weekly_stars[w].append(r["stars"])
        weekly_resp[w].append(1 if r["responseText"] else 0)
        weekly_source[w][r["source"]] += 1
    weeks_sorted = sorted(weekly_counts.keys())
    weekly = [
        {
            "week": w,
            "count": weekly_counts[w],
            "avgStars": round(statistics.mean(weekly_stars[w]), 2) if weekly_stars[w] else None,
            "respRate": round(statistics.mean(weekly_resp[w]), 3),
            "bySource": dict(weekly_source[w]),
        }
        for w in weeks_sorted
    ]

    # recency
    if reviews:
        latest = max(r["publishedAtDt"] for r in reviews if r.get("publishedAtDt"))
    else:
        latest = datetime.now(timezone.utc)
    cutoff7 = latest - timedelta(days=7)
    cutoff30 = latest - timedelta(days=30)
    recent7 = sum(1 for r in reviews if r.get("publishedAtDt") and r["publishedAtDt"] >= cutoff7)
    recent30 = sum(1 for r in reviews if r.get("publishedAtDt") and r["publishedAtDt"] >= cutoff30)

    return {
        "n": n,
        "avgStars": round(avg_stars, 2),
        "ratingStatus": status_for_rating(avg_stars),
        "responseRate": round(response_rate, 3),
        "responseStatus": status_for_response(response_rate),
        "withResponse": with_resp,
        "withoutResponse": n - with_resp,
        "medianResponseLagDays": round(median_response_lag, 1) if median_response_lag else None,
        "negatives": len(negatives),
        "unansweredNegatives": len(unanswered_negatives),
        "subratings": sub_avg,
        "starsDist": {str(k): v for k, v in sorted(stars_dist.items())},
        "weekly": weekly,
        "recent7": recent7,
        "recent30": recent30,
        "sourceBreakdown": source_breakdown,
    }


# ---------- AI brief detectors --------------------------------------------

def build_ai_brief(per_loc, network, locations_order, recent_negatives, source_findings):
    lines: list[dict] = []

    # Opener
    bad_locs = [l for l in locations_order
                if per_loc[l]["responseStatus"] == "RED" or per_loc[l]["ratingStatus"] == "RED"]
    yellow_locs = [l for l in locations_order
                   if per_loc[l]["responseStatus"] == "YELLOW" or per_loc[l]["ratingStatus"] == "YELLOW"]

    if not bad_locs and not yellow_locs:
        opener = "All locations are operating in the green band on both rating and response coverage."
    elif bad_locs:
        opener = (
            f"Network response coverage is the dominant story this period: "
            f"{len(bad_locs)} location(s) are running RED on at least one of the two primary KPIs "
            f"({', '.join(bad_locs)}). The {network['avgStars']}★ network average is masking an operational gap."
        )
    else:
        opener = (
            f"Ratings are healthy across the network. "
            f"{len(yellow_locs)} location(s) need attention on response coverage: "
            f"{', '.join(yellow_locs)}."
        )
    lines.append({"kind": "exec", "text": opener})

    # Source findings come first because they're the new bombshell
    for f in source_findings:
        lines.append(f)

    # Network response gap
    if network["responseRate"] < RESPONSE_GREEN:
        gap_pct = round((1 - network["responseRate"]) * 100)
        lines.append({
            "kind": "high",
            "text": (
                f"Network-wide response rate is {round(network['responseRate']*100)}% across all sources — "
                f"{gap_pct}% of reviews ({network['withoutResponse']} of {network['n']}) "
                f"have not received an owner reply. This is the single highest-leverage "
                f"behavioural change available this period."
            ),
            "priority": 1,
        })

    # Worst response location
    worst_resp = min(locations_order, key=lambda l: per_loc[l]["responseRate"])
    if per_loc[worst_resp]["responseRate"] < RESPONSE_YELLOW:
        m = per_loc[worst_resp]
        lines.append({
            "kind": "high",
            "text": (
                f"{worst_resp} is the response-coverage outlier at "
                f"{round(m['responseRate']*100)}% ({m['withResponse']}/{m['n']}). "
                f"Even a one-week catch-up moves the needle materially."
            ),
            "priority": 2,
        })

    # Best performer
    best_rating = max(locations_order, key=lambda l: per_loc[l]["avgStars"])
    lines.append({
        "kind": "high",
        "text": (
            f"{best_rating} carries the strongest average at "
            f"{per_loc[best_rating]['avgStars']}★ across {per_loc[best_rating]['n']} reviews — "
            f"worth pulling the named servers from those reviews and recognising them in the next manager huddle."
        ),
        "priority": 3,
    })

    # Sub-rating diagnostic — works whichever subratings dimension exists
    for loc in locations_order:
        m = per_loc[loc]
        subs = m.get("subratings", {})
        if "Food" in subs and "Service" in subs:
            gap = subs["Food"] - subs["Service"]
            if gap >= 0.30:
                lines.append({
                    "kind": "low",
                    "text": (
                        f"{loc} shows a Food/Service gap of {round(gap, 2)} points "
                        f"(food {subs['Food']}, service {subs['Service']}). "
                        f"That points to floor staffing or training, not the kitchen."
                    ),
                    "priority": 3,
                })

    # Unanswered negatives across the network
    if recent_negatives:
        lines.append({
            "kind": "low",
            "text": (
                f"There are {len(recent_negatives)} negative review(s) "
                f"(≤{NEGATIVE_STARS}★) without an owner response across the network — "
                f"these should be triaged today. The Recent Negatives queue on the Cockpit lists them."
            ),
            "priority": 2,
        })

    # Coaching focus
    if bad_locs:
        focus = (
            "Coaching focus: prioritise response-coverage catch-up at "
            f"{', '.join(bad_locs)} this week. A 30-minute block per GM clears the backlog."
        )
    elif yellow_locs:
        focus = (
            "Coaching focus: shore up the YELLOW locations before they slip — "
            f"{', '.join(yellow_locs)} only need a small lift to return to green."
        )
    else:
        focus = (
            "Coaching focus: maintain the cadence. With all locations green, the next "
            "lever is response *speed* — current median is documented on each location's tile."
        )
    lines.append({"kind": "focus", "text": focus})

    return lines


def derive_source_findings(network) -> list[dict]:
    """Extract any source-level pattern that should headline the brief (e.g. 0% on a platform)."""
    findings: list[dict] = []
    for src, m in network["sourceBreakdown"].items():
        if m["respRate"] == 0 and m["n"] >= 5:
            findings.append({
                "kind": "high",
                "priority": 1,
                "text": (
                    f"{src} response rate is 0% — none of the {m['n']} {src} reviews have an owner reply. "
                    f"This platform appears to be unmonitored. Average score on {src} is "
                    f"{m['avgStars']}★, so the silence is not because there's nothing to thank."
                ),
            })
        elif m["respRate"] < RESPONSE_YELLOW and m["n"] >= 10:
            findings.append({
                "kind": "high",
                "priority": 2,
                "text": (
                    f"{src} response rate is {round(m['respRate']*100)}% "
                    f"({m['responded']}/{m['n']}) — meaningfully below network target."
                ),
            })
    return findings


# ---------- main ----------------------------------------------------------

def main():
    print("Discovering Apify datasets…")
    discovered = discover_sources()
    if not discovered:
        raise SystemExit("No dataset_*.json files matched any registered source.")

    # Normalize each source, then merge
    pre_dedup: list[dict] = []
    norm_counts: dict[str, int] = {}
    for source, name, raw in discovered:
        norm = SOURCE_REGISTRY[source]["normalizer"](raw)
        pre_dedup.extend(norm)
        norm_counts[source] = norm_counts.get(source, 0) + len(norm)
        print(f"  [norm] {name} -> {len(norm)} normalized")

    # Global dedup
    all_reviews, dedup_stats = dedupe_reviews(pre_dedup)
    print()
    print("Dedup stats:")
    for k, v in sorted(dedup_stats.items()):
        print(f"  {k:>40}: {v}")
    print(f"  {'pre-dedup total':>40}: {len(pre_dedup)}")
    print(f"  {'kept after dedup':>40}: {len(all_reviews)}")

    # Reviews actually retained per source (post-dedup)
    raw_by_source: dict[str, int] = dict(Counter(r["source"] for r in all_reviews))

    # Per-location aggregation
    by_loc_groups: dict[str, list[dict]] = defaultdict(list)
    for r in all_reviews:
        by_loc_groups[r["loc"]].append(r)
    locations_order = sorted(by_loc_groups.keys())

    per_loc_metrics: dict[str, Any] = {}
    per_loc_reviews_serializable: dict[str, list[dict]] = {}
    recent_negatives: list[dict] = []

    def serialize(r: dict) -> dict:
        return {
            "source": r["source"],
            "loc": r["loc"],
            "stars": r["stars"],
            "text": r["text"],
            "title": r.get("title", ""),
            "publishedAt": r["publishedAt"],
            "responseText": r["responseText"],
            "responseAt": r["responseAt"].isoformat() if r.get("responseAt") else None,
            "subratings": r.get("subratings") or {},
            "context": r.get("context") or {},
            "tripType": r.get("tripType"),
            "reviewer": r.get("reviewer") or {},
            "url": r.get("url"),
            "id": r.get("id"),
            "language": r.get("language"),
            "platformScore": r.get("platformScore"),
            "platformReviewsCount": r.get("platformReviewsCount"),
        }

    for loc in locations_order:
        revs = sorted(by_loc_groups[loc], key=lambda x: x["publishedAtDt"], reverse=True)
        per_loc_metrics[loc] = aggregate_metrics(revs)
        per_loc_reviews_serializable[loc] = [serialize(r) for r in revs]
        for r in per_loc_reviews_serializable[loc]:
            if (r["stars"] or 5) <= NEGATIVE_STARS and not r["responseText"]:
                recent_negatives.append(r)

    network = aggregate_metrics(all_reviews)
    source_findings = derive_source_findings(network)

    # ---------- recent bad reviews (high-priority surface) ----------
    last_dt = max((r["publishedAtDt"] for r in all_reviews if r.get("publishedAtDt")), default=None)
    now = last_dt or datetime.now(timezone.utc)
    cutoff_recent = now - timedelta(days=RECENT_BAD_DAYS)
    cutoff_primary = now - timedelta(days=RECENT_BAD_PRIMARY)

    recent_bad: list[dict] = []
    for r in all_reviews:
        dt = r.get("publishedAtDt")
        stars = r.get("stars")
        if not dt or stars is None:
            continue
        if stars > NEGATIVE_STARS:
            continue
        if dt < cutoff_recent:
            continue
        rec = {
            "source": r["source"],
            "loc": r["loc"],
            "stars": stars,
            "text": r.get("text", ""),
            "title": r.get("title", ""),
            "publishedAt": r["publishedAt"],
            "responseText": r.get("responseText") or "",
            "responseAt": r["responseAt"].isoformat() if r.get("responseAt") else None,
            "ageDays": round((now - dt).total_seconds() / 86400, 1),
            "withinPrimary": dt >= cutoff_primary,
            "url": r.get("url"),
            "reviewer": r.get("reviewer") or {},
            "subratings": r.get("subratings") or {},
            "context": r.get("context") or {},
        }
        recent_bad.append(rec)
    recent_bad.sort(key=lambda r: (not r["withinPrimary"], r["ageDays"]))  # last-7d first, then by age

    recent_bad_by_loc = Counter(r["loc"] for r in recent_bad)
    recent_bad_by_source = Counter(r["source"] for r in recent_bad)

    # Insert a high-priority brief item if any recent bad reviews exist
    if recent_bad:
        primary_count = sum(1 for r in recent_bad if r["withinPrimary"])
        unanswered = sum(1 for r in recent_bad if not r["responseText"])
        worst_loc = recent_bad_by_loc.most_common(1)[0]
        recent_finding = {
            "kind": "high",
            "priority": 0,
            "text": (
                f"{len(recent_bad)} bad review(s) (≤{NEGATIVE_STARS}★) in the last "
                f"{RECENT_BAD_DAYS} days, {primary_count} of them in the last "
                f"{RECENT_BAD_PRIMARY} days. {unanswered} have no owner reply yet. "
                f"Hottest location: {worst_loc[0]} ({worst_loc[1]} bad reviews). "
                f"The Recent Bad Reviews panel on the Cockpit lists each one in full."
            ),
        }
        source_findings = [recent_finding] + source_findings

    brief = build_ai_brief(per_loc_metrics, network, locations_order, recent_negatives, source_findings)

    DATA = {
        "product": "Review Command Center",
        "client": "The Breakfast Company",
        "sources": list(raw_by_source.keys()),
        "sourceCounts": raw_by_source,
        "preDedupCount": len(pre_dedup),
        "dedupRemoved": len(pre_dedup) - len(all_reviews),
        "lastReviewAt": last_dt.isoformat() if last_dt else None,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "ratingGreen": RATING_GREEN,
            "ratingYellow": RATING_YELLOW,
            "responseGreen": RESPONSE_GREEN,
            "responseYellow": RESPONSE_YELLOW,
            "negativeStars": NEGATIVE_STARS,
        },
        "locationsOrder": locations_order,
        "byLocationMetrics": per_loc_metrics,
        "byLocationReviews": per_loc_reviews_serializable,
        "network": network,
        "recentNegatives": sorted(
            recent_negatives,
            key=lambda r: r["publishedAt"] or "",
            reverse=True,
        )[:30],
        "recentBad": recent_bad,
        "recentBadWindow": RECENT_BAD_DAYS,
        "recentBadPrimary": RECENT_BAD_PRIMARY,
        "recentBadByLocation": dict(recent_bad_by_loc),
        "recentBadBySource": dict(recent_bad_by_source),
        "brief": brief,
    }

    chart_js = (HERE / "chart.umd.js").read_text(encoding="utf-8")
    template = (HERE / "template.html").read_text(encoding="utf-8")

    html = template.replace("/*__CHARTJS__*/", chart_js)
    html = html.replace("/*__DATA__*/", json.dumps(DATA, ensure_ascii=False))

    out = HERE / "Review Command Center.html"
    out.write_text(html, encoding="utf-8")
    print()
    print(f"Wrote {out} ({out.stat().st_size/1024:.1f} KB)")
    print(f"Sources: {', '.join(network['sourceBreakdown'].keys())}")
    print(f"Total reviews (post-dedup): {network['n']}  Locations: {len(locations_order)}")
    print(f"Network avg ★: {network['avgStars']}  Response rate: {round(network['responseRate']*100)}%")
    print()
    print("Source breakdown:")
    for s, m in network["sourceBreakdown"].items():
        print(f"  {s:>14}  n={m['n']:>4}  avg={m['avgStars']}  resp={round(m['respRate']*100)}%")
    print()
    print(f"Recent bad reviews (≤{NEGATIVE_STARS}★, last {RECENT_BAD_DAYS}d): {len(recent_bad)}")
    for r in recent_bad:
        flag = "*" if r["withinPrimary"] else " "
        unans = " UNANSWERED" if not r["responseText"] else ""
        print(f"  {flag} {r['publishedAt'][:10]}  {r['stars']}★  {r['source']:>11}  {r['loc']:>22}  {r['ageDays']}d{unans}")


if __name__ == "__main__":
    main()
