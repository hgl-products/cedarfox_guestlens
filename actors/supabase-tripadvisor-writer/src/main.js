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
    return res.json();
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
  if (!scraperTaskId) throw new Error('Missing required input: scraperTaskId (the client\'s TripAdvisor scraper task)');
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

// TripAdvisor's ownerResponse arrives as a string OR an object depending on scraper version.
function extractOwnerResponse(ownerResponse) {
  if (!ownerResponse) return { text: '', date: null };
  if (typeof ownerResponse === 'string') return { text: ownerResponse, date: null };
  if (typeof ownerResponse === 'object') {
    return {
      text: ownerResponse.text || '',
      date: ownerResponse.publishedDate || ownerResponse.date || null,
    };
  }
  return { text: '', date: null };
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
  console.log(`=== supabase-tripadvisor-writer starting | datasetId=${datasetId} | testMode=${testMode} ===`);

  console.log(`Fetching items from dataset ${datasetId}...`);
  let items = await fetchDatasetItems(datasetId, apifyToken);
  console.log(`Fetched ${items.length} items from TripAdvisor dataset.`);

  if (testMode) {
    items = items.slice(0, 5);
    console.log(`TEST MODE: limiting to first ${items.length} items.`);
  }

  const usable = items.filter(item => item.id);
  const noId = items.length - usable.length;
  if (noId > 0) console.log(`Skipping ${noId} items without id.`);

  const rows = usable.map(item => {
    const reply = extractOwnerResponse(item.ownerResponse);
    const user = item.user || {};
    const contributions = user.contributions || {};
    return {
      review_id: String(item.id),
      source_platform: 'tripadvisor',
      place_id: String(item.locationId || ''),
      rating: typeof item.rating === 'number' ? item.rating : null,
      review_text: item.text || '',
      review_date: isoOrNull(item.publishedDate),
      owner_response: reply.text,
      owner_response_date: isoOrNull(reply.date),
      review_url: item.url || '',
      reviewer_name: user.name || user.username || '',
      reviewer_review_count: typeof contributions.totalContributions === 'number'
        ? contributions.totalContributions
        : null,
      likes_count: typeof item.helpfulVotes === 'number' ? item.helpfulVotes : null,
      processed: false,
    };
  });

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
    totalFailures: failures.length,
    failures,
  };

  console.log('\n══════════════════════════════════════════════');
  console.log('REPORT');
  console.log(`Records created   : ${report.totalCreated}`);
  console.log(`Duplicates/skipped: ${report.totalSkipped}`);
  console.log(`Failures          : ${report.totalFailures}`);
  console.log('══════════════════════════════════════════════');

  await Actor.setValue('REPORT', report);
});
