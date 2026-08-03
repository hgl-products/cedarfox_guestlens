import { Actor } from 'apify';

// ── Supabase helpers (client-agnostic: all config comes from task input) ──────

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function supaFetch(supabaseUrl, serviceKey, path, options = {}) {
  const url = `${supabaseUrl}/rest/v1/${path}`;
  const headers = {
    apikey: serviceKey,
    Authorization: `Bearer ${serviceKey}`,
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url, { ...options, headers });
    if (res.status === 429 || res.status >= 500) {
      await sleep(Math.pow(2, attempt) * 1000);
      continue;
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Supabase ${options.method || 'GET'} ${path} → ${res.status}: ${text}`);
    }
    if (res.status === 204) return null;
    // PostgREST returns 201 with an EMPTY body when Prefer: return=minimal —
    // res.json() would throw "Unexpected end of JSON input" on it.
    const body = await res.text();
    return body ? JSON.parse(body) : null;
  }
  throw new Error(`Supabase request to ${path} failed after 5 attempts`);
}

async function insertReviews(supabaseUrl, serviceKey, rows) {
  const inserted = await supaFetch(
    supabaseUrl, serviceKey,
    'reviews?on_conflict=review_id&select=review_id',
    {
      method: 'POST',
      headers: { Prefer: 'resolution=ignore-duplicates,return=representation' },
      body: JSON.stringify(rows),
    },
  );
  return Array.isArray(inserted) ? inserted.length : 0;
}

// ── Apify dataset discovery ───────────────────────────────────────────────────

async function findLatestScraperDatasetId(scraperTaskId, apifyToken) {
  if (!apifyToken) throw new Error('Missing required input: apifyToken (needed to auto-discover the latest scraper run)');
  if (!scraperTaskId) throw new Error('Missing required input: scraperTaskId (the client\'s Google Maps scraper task)');
  const url = `https://api.apify.com/v2/actor-tasks/${scraperTaskId}/runs?token=${apifyToken}&status=SUCCEEDED&desc=true&limit=1`;
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch scraper runs → ${res.status}: ${text}`);
  }
  const data = await res.json();
  const runs = data?.data?.items || [];
  if (runs.length === 0) throw new Error('No SUCCEEDED runs found for the scraper task — nothing to process');
  const latest = runs[0];
  console.log(`Found latest scraper run: id=${latest.id} finishedAt=${latest.finishedAt}`);
  return latest.defaultDatasetId;
}

async function fetchDatasetItems(datasetId, apifyToken) {
  const items = [];
  let offset = 0;
  const limit = 1000;
  while (true) {
    const url = `https://api.apify.com/v2/datasets/${datasetId}/items?token=${apifyToken}&limit=${limit}&offset=${offset}`;
    const res = await fetch(url);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Dataset fetch failed → ${res.status}: ${text}`);
    }
    const batch = await res.json();
    if (!Array.isArray(batch) || batch.length === 0) break;
    items.push(...batch);
    if (batch.length < limit) break;
    offset += batch.length;
  }
  return items;
}

// Postgres timestamptz rejects '' — always null for missing dates.
const isoOrNull = v => (v ? v : null);

// ── Main ──────────────────────────────────────────────────────────────────────

Actor.main(async () => {
  const input = await Actor.getInput();
  const {
    supabaseUrl, supabaseServiceKey, apifyToken,
    scraperTaskId, datasetId: inputDatasetId, testMode = false,
  } = input;

  if (!supabaseUrl) throw new Error('Missing required input: supabaseUrl');
  if (!supabaseServiceKey) throw new Error('Missing required input: supabaseServiceKey');

  const datasetId = inputDatasetId || await findLatestScraperDatasetId(scraperTaskId, apifyToken);
  console.log(`=== supabase-google-writer starting | datasetId=${datasetId} | testMode=${testMode} ===`);

  // Google reviews resolve their location through the CID → locations table.
  // (Yelp/TA resolve at BUILD time via place_id; Google resolves at WRITE time
  // because the dashboard's normalize step expects a location link on Google rows.)
  console.log('Fetching locations from Supabase...');
  const locations = await supaFetch(supabaseUrl, supabaseServiceKey, 'locations?select=id,location_name,google_place_cid');
  const cidToLocation = {};
  for (const loc of locations || []) {
    if (loc.google_place_cid) cidToLocation[String(loc.google_place_cid)] = loc.id;
  }
  console.log(`Loaded ${Object.keys(cidToLocation).length} locations with a Google CID.`);
  if (Object.keys(cidToLocation).length === 0) {
    throw new Error('No locations have google_place_cid set — seed the locations table first (see RUNBOOK Phase 1).');
  }

  console.log(`Fetching items from dataset ${datasetId}...`);
  let items = await fetchDatasetItems(datasetId, apifyToken);
  console.log(`Fetched ${items.length} items from Google dataset.`);

  if (testMode) {
    items = items.slice(0, 5);
    console.log(`TEST MODE: limiting to first ${items.length} items.`);
  }

  const usable = [];
  const unmatched = [];
  let noId = 0;
  for (const item of items) {
    if (!item.reviewId) { noId++; continue; }
    const cid = String(item.cid || '');
    const locationId = cidToLocation[cid];
    if (!locationId) {
      unmatched.push({ reviewId: item.reviewId, cid, title: item.title || '' });
      continue;
    }
    usable.push({ item, locationId });
  }
  if (noId > 0) console.log(`Skipping ${noId} items without reviewId.`);
  if (unmatched.length > 0) {
    console.warn(`⚠️  ${unmatched.length} items have a CID that matches NO location — they were NOT written:`);
    const byCid = {};
    for (const u of unmatched) byCid[u.cid] = (byCid[u.cid] || 0) + 1;
    for (const [cid, n] of Object.entries(byCid)) console.warn(`   cid=${cid}: ${n} reviews (add this CID to the locations table if it's a real store)`);
  }

  const rows = usable.map(({ item, locationId }) => ({
    review_id: item.reviewId,
    source_platform: 'google',
    location_id: locationId,
    place_id: String(item.cid || item.placeId || ''),
    rating: typeof item.stars === 'number' ? item.stars : null,
    review_text: item.text || '',
    review_date: isoOrNull(item.publishedAtDate),
    owner_response: item.responseFromOwnerText || '',
    owner_response_date: isoOrNull(item.responseFromOwnerDate),
    review_url: item.reviewUrl || '',
    reviewer_name: item.name || '',
    reviewer_review_count: typeof item.reviewerNumberOfReviews === 'number' ? item.reviewerNumberOfReviews : null,
    is_local_guide: Boolean(item.isLocalGuide),
    likes_count: typeof item.likesCount === 'number' ? item.likesCount : null,
    processed: false,
  }));

  let totalCreated = 0;
  const failures = [];
  const BATCH_SIZE = 500;

  for (let i = 0; i < rows.length; i += BATCH_SIZE) {
    const batch = rows.slice(i, i + BATCH_SIZE);
    try {
      const created = await insertReviews(supabaseUrl, supabaseServiceKey, batch);
      totalCreated += created;
      console.log(`Batch ${Math.floor(i / BATCH_SIZE) + 1}: inserted ${created}/${batch.length} (rest were duplicates).`);
    } catch (err) {
      console.error(`[ERROR] Supabase insert failed (batch at index ${i}): ${err.message}`);
      for (const row of batch) failures.push({ reviewId: row.review_id, reason: err.message });
    }
  }

  const report = {
    totalCreated,
    totalSkipped: rows.length - totalCreated - failures.length + noId,
    totalUnmatchedLocation: unmatched.length,
    totalFailures: failures.length,
    failures,
  };

  console.log('\n══════════════════════════════════════════════');
  console.log('REPORT');
  console.log(`Records created    : ${report.totalCreated}`);
  console.log(`Duplicates/skipped : ${report.totalSkipped}`);
  console.log(`Unmatched location : ${report.totalUnmatchedLocation}`);
  console.log(`Failures           : ${report.totalFailures}`);
  console.log('══════════════════════════════════════════════');

  await Actor.setValue('REPORT', report);
});
