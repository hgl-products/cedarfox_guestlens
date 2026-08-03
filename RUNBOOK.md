# GuestLens — Cedar Fox Coffee Runbook (Supabase-native, client #2)

Last updated: 2026-08-03 · Status: **🟢 FULLY LIVE — pipeline complete end-to-end.**
243/243 reviews scraped→stored→AI-analyzed. Dashboard on GitHub Pages, nightly
Apify schedules + GitHub Action active. Remaining (non-blocking): owner demo,
staff roster after pilot, password protection when requested, Supabase Pro
upgrade when client pays.

This is the master reference for this client's deployment — the equivalent of
TBC's SESSION_CONTEXT.md. To resume any session: share this file with Claude
and say "read RUNBOOK.md — let's continue."

---

## Architecture

```
Apify scrapers (Google / Yelp / TripAdvisor)          ~18:00 client tz
  → supabase-*-writer actors (dedup via ON CONFLICT)  ~18:20–18:30
    → Supabase Postgres (this client's own project)
      → review-analyzer-supabase (Claude Haiku 4.5)   ~18:45
        → nightly build (GitHub Actions, 3 UTC crons)
          → index.html → StatiCrypt encrypt → GitHub Pages
```

Differences vs the TBC (Airtable) deployment:
- **Supabase Postgres** instead of Airtable — one project **per client**, full isolation
- **No account-level webhook** — Google uses a scheduled writer actor like Yelp/TA
- **No dedup pre-fetch** — `INSERT … ON CONFLICT (review_id) DO NOTHING` in the DB
- **Client-agnostic actors** — all IDs/keys via task input; client #3 = new tasks, zero code copies
- **client_config.py** — the only per-client Python file (brand, crosswalk, exclusions)
- **Staff roster lives in Supabase** — edited in Supabase Studio grid, seeded via `roster_import.py`

---

## Phase 0 — Client intake ✅ (collected 2026-08-01)

| # | Item | Value |
|---|---|---|
| 1 | Client display name | **Cedar Fox Coffee** — breakfast restaurant / coffee shop, 1952 Main Street, Sarasota, FL 34236 · https://cedarfoxcoffee.com/ |
| 2 | Locations | Single location: `Cedar Fox Coffee` (short: "Main Street") |
| 3 | Google CID | **`5689274273260063895`** (decimal of `0x4ef45ccef76f4c97` from the Maps place pair; verified against https://maps.app.goo.gl/F2LgHEDrFX4zARwS9) |
| 4 | Yelp URL | https://www.yelp.com/biz/cedar-fox-coffee-sarasota-2 (alias `cedar-fox-coffee-sarasota-2`) |
| 5 | TripAdvisor URL | https://www.tripadvisor.com/Restaurant_Review-g34618-d32707369-Reviews-Cedar_Fox-Sarasota_Florida.html (id `32707369`; owner shared the .es locale — same id) |
| 6 | Logo | ✅ `assets/cedarfox-logo.png` (pulled from their website) |
| 7 | Staff roster CSV | **None yet** — pilot launches without it; dashboard falls back to AI-extracted staff mentions. Roster expected after pilot. |
| 8 | Dashboard password | **None for pilot** (owner demo) — StatiCrypt step is commented out in the workflow, ready to re-enable |
| 9 | Client timezone | America/New_York (Sarasota — same as TBC; default crons carry over) |
| 10 | Excluded locations | None |
| 11 | Platforms | Google + Yelp + TripAdvisor (all three confirmed present) |

**Pilot goal:** show the owner what GuestLens can present using ONLY scraped
public data — no input from them at all.

Where each intake item lands:
- 1, 6 → `client_config.py` BRAND + `.github/workflows/build.yml` StatiCrypt title
- 2 → `client_config.py` SHORT_LOC + Supabase `locations` table
- 3–5 → Supabase `locations` table (CID/urls) + Apify scraper task inputs
- 7 → `roster_import.py` + agree crosswalk tokens → `client_config.py` CROSSWALK
- 8 → GitHub secret `DASHBOARD_PASSWORD`
- 9 → Apify schedule times + workflow cron math (below)

---

## Phase 1 — Foundation

1. Create Supabase project (free tier OK to start; Pro $25/mo once client pays).
   Naming convention: name the project after the CUSTOMER, not the product
   (e.g. `cedarfox-hub`), since future products for the same customer can share
   the project — each product gets its own Postgres schema if needed later.
   Renaming an existing project is safe: Project Settings (gear) → General →
   Project name → Save. The name is only a label — the project URL/ref and
   keys never change.
2. SQL Editor (left sidebar, `SQL` icon) → New query → paste `supabase/schema.sql`
   → Run. Expected result: "Success. No rows returned". Idempotent, safe to re-run.
   macOS tip to copy a file to the clipboard: `pbcopy < supabase/schema.sql`
3. Same way, run `supabase/seed_locations.sql` (Cedar Fox row, already written).
   Verify: Table Editor (left sidebar, grid icon) → 4 tables exist, `locations`
   has 1 row with google_place_cid `5689274273260063895`.
4. ~~Import roster~~ — skipped for the pilot (no roster yet). When it arrives:
   `python roster_import.py roster.csv --dry-run` first, then without the flag,
   then fill CROSSWALK in `client_config.py`.
5. ✅ `client_config.py` filled and logo in `assets/cedarfox-logo.png` (done 2026-08-01).

**Gate:** `SUPABASE_URL=… SUPABASE_SERVICE_KEY=… python fetch_supabase.py`
prints correct row counts and Airtable-shaped sample fields for all 4 tables.

Secrets discipline (same as TBC): the `service_role` key bypasses RLS — it goes
ONLY into (a) Apify task saved inputs (secret fields), (b) GitHub repo secrets,
(c) your local shell for one-off runs. Never in any committed file.

## Phase 2 — Ingest + backfill

1. Create scraper tasks (same store actors as TBC):
   Google `Xb8osYTtOjlsgI6k9` · Yelp `zzaj8C9vahbUG3T0U` · TA `Hvp4YfFGyLM635Q2F`.
   For backfill set high `maxReviews` (e.g. 500) and no start-date limit.
2. Push the writers (from each folder under `actors/`): `apify push --force`.
3. Create one task per writer. **Saved task input** (never console-run inputs —
   hard-won TBC lesson): `supabaseUrl`, `supabaseServiceKey` (secret),
   `apifyToken` (secret), `scraperTaskId` = the matching scraper task from step 1.
4. Run each scraper once → run each writer with `testMode: true` → inspect log →
   full run (optionally with explicit `datasetId`).
5. Flip scrapers to incremental (`reviewsStartDate: "1 day"` for Google; per-actor
   equivalents for Yelp/TA).

**Gates:**
- `reviews` count matches dataset count minus dupes (writer REPORT shows both)
- re-running a writer inserts 0 rows (ON CONFLICT proof)
- every Google row has `location_id` (writer logs unmatched CIDs loudly)

## Phase 3 — Analyzer

1. From `actors/review-analyzer-supabase/`: `apify push --force`.
2. Task with saved input: `supabaseUrl`, `supabaseServiceKey`, `anthropicApiKey`.
3. `testMode: true` run (5 reviews, eyeball the JSON) → full backfill run.
   Timeout math from TBC: ~2.2s/review; >1,600 reviews → start the run with an
   explicit `timeout` query param.

**Gate:** `review_analysis` count == count of `processed=true` reviews;
spot-check 5 rows in Studio.

## Phase 4 — Dashboard

```bash
SUPABASE_URL=… SUPABASE_SERVICE_KEY=… OUTPUT_FILE=index_preview.html python build_v3.py
open index_preview.html
```

**Gate:** brand + logo correct, all locations present, Staff Stars resolving
through CROSSWALK, dish intelligence populated, period filters work.

## Phase 5 — Automation

1. Apify schedules in the client's timezone: scrapers 18:00 → TA writer 18:20 →
   Yelp writer 18:30 → **Google writer 18:35** → analyzer 18:45.
2. New GitHub repo (public for free Pages) → push this folder → Settings:
   - Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DASHBOARD_PASSWORD`
   - Pages: branch `main`, root `/`
3. Update StatiCrypt `--template-title` in `.github/workflows/build.yml`.
4. Trigger `workflow_dispatch` once; confirm the encrypted page + password work.

**Cron math** (only if client tz ≠ US Eastern): pipeline finishes ~19:00 client
time; pick three UTC slots ≈ 19:15 / 20:05 / 21:37 client time, using the
SUMMER offset (the winter drift is covered by the extra slots). Keep odd minutes
— GitHub drops on-the-hour crons most often. Extra fires are free (idempotent
commit step).

**Gate (next evening):** each writer log shows yesterday's dataset date ·
analyzer processed >0 (or correctly 0) · dashboard "Updated" timestamp advanced ·
password page works with 30-day remember.

---

## Component inventory (as of 2026-08-01)

| Component | ID / value | Notes |
|---|---|---|
| Supabase project | name `cedarfox-hub` (renamed from Cedar_Fox_Coffee) | schema + Cedar Fox location row seeded ✅ |
| Google scraper task | `gYymoWu39GuMKQNNM` — `cedarfox-google-scraper` | backfill run ✅ 217 items (dataset `eImWH2rLvFu1SceS2`) |
| Yelp scraper task | `0raKqcQmJG0SvoqQg` — `cedarfox-yelp-scraper` | backfill run ✅ 23 items (dataset `B426Iq9A2vpl2wJCo`) |
| TA scraper task | `JjLkcnbXGwJEgZLXm` — `cedarfox-tripadvisor-scraper` | backfill run ✅ 3 items (dataset `zgcEnX4dC8wadRsAK`) |
| supabase-google-writer | actor `005ykRorb5sPZVYOd` / task `2sCP63Yvp6jwOLToJ` | ⏳ secrets: https://console.apify.com/actor-tasks/2sCP63Yvp6jwOLToJ/input |
| supabase-yelp-writer | actor `8Gq6PKMfsBrtwmms1` / task `ycL76tVqPDld9oGdk` | ⏳ secrets: https://console.apify.com/actor-tasks/ycL76tVqPDld9oGdk/input |
| supabase-tripadvisor-writer | actor `AsCbYuqVZ3r6W17Jh` / task `aUeSfdR58VJaLZi0V` | ⏳ secrets: https://console.apify.com/actor-tasks/aUeSfdR58VJaLZi0V/input |
| review-analyzer-supabase | actor `CT2BDSJUhYdKeJzpX` / task `fIqvdviwnHsvPsRqH` | ⏳ secrets + Anthropic key: https://console.apify.com/actor-tasks/fIqvdviwnHsvPsRqH/input |
| Schedule: google-scraper 18:00 ET | `a2n3Drhicl7GnLecn` | daily, America/New_York |
| Schedule: yelp-scraper 18:00 ET | `fHlYwfVVcirIy7D4n` | daily |
| Schedule: ta-scraper 18:00 ET | `o4dpmXBenYbsB3ZNH` | daily |
| Schedule: ta-writer 18:20 ET | `Uaisy5lwjiD12LQIi` | daily |
| Schedule: yelp-writer 18:30 ET | `Y26KDa9fKQuGlQg0G` | daily |
| Schedule: google-writer 18:35 ET | `BZSPbHI4h5nExCN2H` | daily |
| Schedule: analyzer 18:45 ET | `xwd4BmbhZ5WSBUnLO` | daily |
| GitHub repo | `hgl-products/cedarfox_guestlens` | secrets `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` set; workflow crons 23:15/00:05/01:37 UTC |
| **Dashboard URL** | **https://hgl-products.github.io/cedarfox_guestlens/** | no password (pilot); StatiCrypt step commented in workflow |

## Work log — 2026-08-03 (launch day)

- Secrets: Julian put SUPABASE_URL / SUPABASE_SERVICE_KEY / ANTHROPIC_API_KEY in
  `~/.guestlens-cedarfox.env` (home dir, outside git). Claude saved them into all
  4 Apify tasks via API (PUT task input — encrypts `isSecret` fields server-side).
  Lesson: pasting in the Apify console UI twice failed to save; the API route is
  reliable and verifiable (GET input → "ENCRYPTED").
- Note: Julian's SUPABASE_URL initially included `/rest/v1/` — the code normalizes
  it, but the canonical value is the bare project URL.
- Writers verified: testMode 5 rows → dedup re-run 0 rows (ON CONFLICT proof) →
  full runs 243/243, all 217 Google rows location-matched via CID.
- **Bug found+fixed:** `supaFetch` in all 4 actors did `res.json()` on PostgREST's
  empty 201 body (`Prefer: return=minimal`) → "Unexpected end of JSON input".
  Fixed to read text-then-parse; re-pushed all 4. No data lost (`processed` flips
  only after a successful analysis write).
- Analyzer: testMode 5/5 → full run 238 remaining, 0 failures, **$0.42 total**.
- Dashboard: logo bumped 30px→36px (+20%) at Julian's request. Preview approved.
- Made permanent: scrapers flipped to incremental (Google `reviewsStartDate: "1 day"`
  maxReviews 100; Yelp/TA max 50 — writers dedup anyway), 7 schedules created,
  repo `cedarfox_guestlens` created + pushed, secrets set via API (pynacl sealed
  box), Pages enabled, workflow_dispatch smoke test run.

**Remaining / future:** owner demo + feedback · staff roster CSV after pilot
(roster_import.py + CROSSWALK in client_config.py) · enable StatiCrypt password
when requested (uncomment workflow step + add DASHBOARD_PASSWORD secret) ·
Supabase Pro ($25/mo) when client pays · watch first scheduled night end-to-end.

## API quick reference

```bash
# Row counts (replace $URL/$KEY)
curl -s "$URL/rest/v1/reviews?select=count" -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Prefer: count=exact" -I | grep content-range
curl -s "$URL/rest/v1/reviews?processed=eq.false&select=count" -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Prefer: count=exact" -I | grep content-range

# Run a task manually
curl -X POST "https://api.apify.com/v2/actor-tasks/{TASK_ID}/runs?token=$APIFY_TOKEN"

# Local dashboard preview
SUPABASE_URL=… SUPABASE_SERVICE_KEY=… OUTPUT_FILE=index_preview.html python build_v3.py
```

## Hard-won lessons carried over from TBC

- **Saved task input, not console runs** — encrypted secrets live per-task; a
  console "Run with input" does not persist them.
- **`desc=true` on run discovery** — without it you get the OLDEST run.
- **GitHub cron is best-effort** — runs fire late by minutes-to-hours; a
  "missed" slot is normal, which is why there are three.
- **Google reviews need a location link at write time** — unmatched CID = the
  review silently vanishes from the dashboard; the google-writer logs these loudly.
- **Postgres `timestamptz` rejects `''`** — writers insert `null` for missing dates.
- **`git commit --no-edit` on merges** — never let git open vim.
- **Supabase free tier pauses after ~7 idle days** — nightly writers keep it
  alive, but a broken-for-a-week pipeline compounds into a paused DB. Move to
  Pro when the client is paying.

## What is intentionally NOT here

- Anything TBC: its Airtable base, webhook, actors, tasks, repo — untouched.
- The TBC→Supabase migration — separate future effort; this deployment is its
  proving ground.

## Replication Checklist v2 (for client #3+)

1. Copy this folder → rename → empty `client_config.py` values → new git repo.
2. New Supabase project → `schema.sql` → seed locations → roster import.
3. New scraper tasks on the 3 store actors (client URLs, client tz).
4. New tasks on the SAME 4 actors (no `apify push` needed) with the new
   project's `supabaseUrl`/`supabaseServiceKey`/`scraperTaskId`.
5. Backfill (Phase 2–3 above) → dashboard (Phase 4) → automation (Phase 5).
6. Fill the component inventory table; hand the client their URL + password.
