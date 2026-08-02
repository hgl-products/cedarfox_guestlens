#!/usr/bin/env python3
"""
GuestLens — per-client configuration: CEDAR FOX COFFEE (client #2).

THE ONLY Python file that changes between clients. All other .py files are
client-agnostic and copied verbatim for each new deployment.
"""

# ---------- branding (rendered into the dashboard header/footer) ---------------

BRAND = {
    "name":    "GuestLens",
    "tagline": "Review Intelligence",
    "client":  "Cedar Fox Coffee",
    "builtBy": "Humble Goat Labs",
    "logoPath": "assets/cedarfox-logo.png",  # embedded as data URI at build time
}

# ---------- locale ---------------------------------------------------------------

# Sarasota, FL — same as TBC, so the default Apify schedule times and the
# workflow's cron slots carry over unchanged.
TIMEZONE = "America/New_York"

# ---------- dashboard scope ------------------------------------------------------

# Single-location client — nothing to exclude.
EXCLUDED_LOCATIONS: set[str] = set()

# Single location; the full name is short enough everywhere.
SHORT_LOC: dict[str, str] = {
    "Cedar Fox Coffee": "Main Street",
}

# ---------- Staff Stars crosswalk -------------------------------------------------

# No staff roster yet (pilot launches without one — the dashboard gracefully
# falls back to AI-extracted staff mentions attributed by review location).
# When the owner provides a roster CSV after the pilot: import it with
# roster_import.py and map its location tokens here, e.g. {"main street": "Cedar Fox Coffee"}.
CROSSWALK: dict[str, str | None] = {}
