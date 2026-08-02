#!/usr/bin/env python3
"""
Roster-based Staff Stars resolver — the single source of truth for turning
raw Staff_Mentioned strings into location-correct, verified staff attributions.

Pure logic, no network: both staff_resolve.py (validation harness) and
build_v3.py (production build) import from here, so what was validated is
byte-for-byte what ships.

The review's location is a CROSS-CHECK, never the source of truth:
  - a name that resolves to one roster person → that person's roster store
  - a shared first name → disambiguated by the review's store (single-location
    staff preferred over floating managers)
  - anything unresolved → the "needs review" bucket, never a silent guess.
Aliases (maintained by managers in Airtable) are consumed at build time, so the
leaderboard self-improves nightly with no code change.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter, defaultdict

# roster "locations_can_work_at" token (normalized) → dashboard Location_Name.
# Client-specific — lives in client_config.py; non-storefront tokens map to None.
from client_config import CROSSWALK

# generic role words the AI sometimes extracts as a "name" — never real staff
STOPWORDS: set[str] = {
    "manager", "gm", "host", "hostess", "server", "waiter", "waitress", "busser",
    "barista", "bartender", "cook", "chef", "dishwasher", "cashier", "owner",
    "staff", "team", "waitstaff", "crew", "everyone", "everybody", "someone",
    "gentleman", "lady", "guy", "girl", "kid", "employee", "worker",
}


def norm(s: str) -> str:
    # Do NOT strip a leading "the " here — location tokens like "The Landings"
    # flow through this and must keep the article for the crosswalk.
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = s.strip().casefold()
    s = re.sub(r"[.’']", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_mention(m: str) -> tuple[str, str | None]:
    """'Sara H' -> ('sara','h'); 'the hostess' -> ('hostess', None); 'Betsy' -> ('betsy', None)."""
    n = norm(m)
    if n.startswith("the "):          # role phrases only: "the hostess"
        n = n[4:]
    toks = n.split()
    if len(toks) == 2 and len(toks[1]) == 1:
        return toks[0], toks[1]
    return (toks[0] if toks else n), None


def build_people(staff_records: list[dict]) -> list[dict]:
    """Airtable roster rows → normalized people. Empty/malformed rows skipped."""
    people = []
    for rec in staff_records:
        f = rec.get("fields", {})
        first = norm(f.get("First Name"))
        if not first:
            continue
        last = norm(f.get("Last Name"))
        dash_locs, seen = [], set()
        for tok in (f.get("Locations Can Work At") or "").split(","):
            key = norm(tok)
            if not key:
                continue
            mapped = CROSSWALK.get(key, "__STORE__")   # unknown token = assume a store we don't map → skip
            if mapped and mapped != "__STORE__" and mapped not in seen:
                seen.add(mapped)
                dash_locs.append(mapped)
        aliases = {norm(a) for a in (f.get("Aliases") or "").split(",") if norm(a)}
        people.append({
            "full": (f.get("Full Name") or f"{f.get('First Name', '')} {f.get('Last Name', '')}").strip(),
            "first": first,
            "last_init": last[0] if last else None,
            "names": {first} | aliases,
            "locs": dash_locs,
            "single": len(dash_locs) == 1,
            "dept": f.get("Department", ""),
        })
    return people


def build_index(people: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    for p in people:
        for nm in p["names"]:
            idx[nm].append(p)
    return idx


def resolve(mention: str, rloc: str | None, index: dict[str, list[dict]]):
    """Returns (status, person|None, store|None). status ∈
    verified | unmatched | ambiguous | loc-unclear | role."""
    first, init = parse_mention(mention)
    if not first or first in STOPWORDS or len(first) < 2:
        return ("role", None, None)
    cands = list(index.get(first, []))
    if not cands:
        return ("unmatched", None, None)
    if init:
        byi = [p for p in cands if p["last_init"] == init]
        if not byi:
            return ("unmatched", None, None)
        cands = byi
    if len(cands) > 1:
        here = [p for p in cands if rloc and rloc in p["locs"]]
        if len(here) == 1:
            cands = here
        elif len(here) > 1:
            singles = [p for p in here if p["single"]]
            cands = singles if len(singles) == 1 else here
        if len(cands) != 1:
            return ("ambiguous", None, None)
    p = cands[0]
    if p["single"]:
        return ("verified", p, p["locs"][0])
    if rloc and rloc in p["locs"]:
        return ("verified", p, rloc)
    return ("loc-unclear", p, None)


def suggest_alias(name: str, people: list[dict]) -> str:
    """Best-effort hint for a needs-review name: typo neighbor OR nickname prefix
    (dani→daniela). A human confirms via Aliases. Returns full name or ''."""
    all_names = {nm for p in people for nm in p["names"]}
    close = difflib.get_close_matches(name, list(all_names), n=1, cutoff=0.8)
    hit = close[0] if close else None
    if not hit and len(name) >= 3:
        for p in people:
            if p["first"].startswith(name) or name.startswith(p["first"]):
                hit = p["first"]
                break
    if not hit:
        return ""
    who = next((p for p in people if hit in p["names"]), None)
    return who["full"] if who else ""


def compute_staff_stars(reviews: list[dict], analysis_map: dict, people: list[dict],
                        index: dict[str, list[dict]], top: int = 25) -> dict:
    """For a review subset, return verified leaderboard, needs-review list, and
    per-store verified-mention totals (the competition's second metric).

    `reviews` are normalized dicts with 'id' (Review_ID) and 'loc' (dashboard
    Location_Name). Mentions come from analysis_map[id]['Staff_Mentioned'].
    """
    verified: dict[tuple[str, str], int] = defaultdict(int)  # (full, store) -> count
    person_meta: dict[str, dict] = {}
    mentions_by_loc: Counter = Counter()
    needs: dict[str, dict] = defaultdict(lambda: {"count": 0, "locs": Counter(), "reason": ""})

    for r in reviews:
        rid, rloc = r.get("id", ""), r.get("loc")
        raw = (analysis_map.get(rid, {}).get("Staff_Mentioned") or "").strip()
        if not raw:
            continue
        for mention in [m.strip() for m in raw.split(",") if m.strip()]:
            status, p, store = resolve(mention, rloc, index)
            if status == "verified":
                verified[(p["full"], store)] += 1
                person_meta[p["full"]] = {"first": p["first"].title(), "dept": p["dept"]}
                mentions_by_loc[store] += 1
            elif status in ("unmatched", "ambiguous", "loc-unclear"):
                key = norm(mention)
                e = needs[key]
                e["count"] += 1
                e["reason"] = status
                if rloc:
                    e["locs"][rloc] += 1

    leaderboard = sorted(
        ({"name": person_meta[full]["first"], "full": full, "mentions": cnt,
          "loc": store, "verified": True}
         for (full, store), cnt in verified.items()),
        key=lambda x: -x["mentions"],
    )[:top]

    needs_review = sorted(
        ({"name": name, "mentions": e["count"], "reason": e["reason"],
          "loc": e["locs"].most_common(1)[0][0] if e["locs"] else "",
          "suggestion": suggest_alias(name, people)}
         for name, e in needs.items()),
        key=lambda x: -x["mentions"],
    )[:30]

    return {
        "staffLeaderboard": leaderboard,
        "staffNeedsReview": needs_review,
        "staffMentionsByLoc": dict(mentions_by_loc),
    }
