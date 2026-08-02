-- ============================================================
-- GuestLens — location seed: CEDAR FOX COFFEE
-- Run AFTER schema.sql in the Supabase SQL Editor.
-- Safe to re-run (upsert on location_name).
--
-- google_place_cid = decimal form of the second hex in the Maps
-- place pair 0x88c34195cf670dc3:0x4ef45ccef76f4c97
-- (verified against https://maps.app.goo.gl/F2LgHEDrFX4zARwS9)
-- ============================================================

insert into locations (location_name, google_place_cid, yelp_url, tripadvisor_url)
values (
  'Cedar Fox Coffee',
  '5689274273260063895',
  'https://www.yelp.com/biz/cedar-fox-coffee-sarasota-2',
  'https://www.tripadvisor.com/Restaurant_Review-g34618-d32707369-Reviews-Cedar_Fox-Sarasota_Florida.html'
)
on conflict (location_name) do update set
  google_place_cid = excluded.google_place_cid,
  yelp_url         = excluded.yelp_url,
  tripadvisor_url  = excluded.tripadvisor_url;
