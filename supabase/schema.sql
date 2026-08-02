-- ============================================================
-- GuestLens — Supabase schema (Client #2, template for all
-- future Supabase-native clients)
--
-- Run this ONCE in the Supabase SQL Editor of a fresh project.
-- Safe to re-run: uses IF NOT EXISTS everywhere.
-- ============================================================

-- ── Locations ────────────────────────────────────────────────
create table if not exists locations (
  id               uuid primary key default gen_random_uuid(),
  location_name    text not null unique,
  google_place_cid text,          -- CID used to match Google scraper items
  yelp_url         text,          -- https://www.yelp.com/biz/<alias>
  tripadvisor_url  text,          -- contains -d<locationId>-
  created_at       timestamptz not null default now()
);

-- ── Reviews (machine-written by the writer actors) ──────────
create table if not exists reviews (
  id                    bigint generated always as identity primary key,
  review_id             text not null unique,   -- platform-native id; THE dedup key
  source_platform       text not null,          -- 'google' | 'yelp' | 'tripadvisor'
  location_id           uuid references locations(id),
  place_id              text,                   -- yelp alias / TA locationId / google cid
  rating                numeric(2,1),
  review_text           text not null default '',
  review_date           timestamptz,
  owner_response        text not null default '',
  owner_response_date   timestamptz,
  review_url            text,
  reviewer_name         text,
  reviewer_review_count int,
  is_local_guide        boolean not null default false,
  likes_count           int,
  processed             boolean not null default false,
  created_at            timestamptz not null default now()
);

create index if not exists reviews_unprocessed_idx on reviews (id) where not processed;
create index if not exists reviews_date_idx        on reviews (review_date);
create index if not exists reviews_source_idx      on reviews (source_platform);
create index if not exists reviews_location_idx    on reviews (location_id);

-- ── Review analysis (written by review-analyzer-supabase) ───
create table if not exists review_analysis (
  id                          bigint generated always as identity primary key,
  review_id                   text not null unique references reviews(review_id),
  sentiment                   text,      -- Positive / Neutral / Negative / Mixed
  sentiment_score             smallint,  -- -5 .. +5
  urgency                     text,      -- None / Low / Medium / High / Critical
  is_actionable               boolean,
  primary_theme               text,
  all_themes                  text,      -- comma-separated
  food_mentioned              text,      -- comma-separated dish names
  staff_mentioned             text,      -- comma-separated staff names
  staff_sentiment             text,      -- Praised / Criticized / Mixed / None
  complaint_category          text,
  praise_category             text,
  mentions_competitor         boolean,
  repeat_customer             boolean,
  mentions_refund_or_recovery boolean,
  suggested_response_tone     text,
  key_points_to_address       text,
  analysis_summary            text,
  analyzed_at                 timestamptz not null default now(),
  analysis_model              text
);

-- ── Staff roster (human-edited via Supabase Studio / CSV import) ──
create table if not exists staff_roster (
  id                    bigint generated always as identity primary key,
  first_name            text,
  last_name             text,
  full_name             text,
  aliases               text,   -- comma-separated nicknames
  locations_can_work_at text,   -- comma-separated tokens matching client_config CROSSWALK
  department            text,
  employment_status     text,   -- e.g. Active / Inactive
  updated_at            timestamptz not null default now()
);

-- ── Security: RLS on, NO policies ────────────────────────────
-- service_role bypasses RLS (used by actors + build script).
-- anon/authenticated keys can read NOTHING. The dashboard is a
-- static file and never talks to this database.
alter table locations       enable row level security;
alter table reviews         enable row level security;
alter table review_analysis enable row level security;
alter table staff_roster    enable row level security;
