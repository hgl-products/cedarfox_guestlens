import { Actor } from 'apify';

const MODEL = 'claude-haiku-4-5';

const SYSTEM_PROMPT = `You are a restaurant review analyst. Analyze ONE customer review and return ONLY a JSON object — no preamble, no markdown, no explanation.
You will be given: the review text, the star rating (1–5), the platform it came from, and the restaurant location name.
RULES:
- Return ONLY valid JSON matching the schema below, using the EXACT allowed values for each enumerated field.
- If the review has no text (rating-only), infer what you can from the star rating and set text-derived fields to "" or "None" as appropriate.
- Never invent staff names, dishes, or competitors that aren't actually in the review.
- Base every field only on what the review actually says.
Return exactly this JSON shape:
{
  "sentiment": "Positive | Neutral | Negative | Mixed",
  "sentiment_score": integer from -5 (worst) to +5 (best),
  "urgency": "None | Low | Medium | High | Critical",
  "is_actionable": true or false,
  "primary_theme": "Food | Service | Speed | Cleanliness | Value | Atmosphere | Accuracy | Other",
  "all_themes": "comma-separated list of every theme touched, e.g. Food, Service",
  "food_mentioned": "specific dishes/items named, comma-separated; empty string if none",
  "staff_mentioned": "names of staff named in the review; empty string if none",
  "staff_sentiment": "Praised | Criticized | Mixed | None",
  "complaint_category": "Wait time | Order accuracy | Food quality | Staff attitude | Cleanliness | Price | Other | None",
  "praise_category": "Food | Service | Speed | Value | Atmosphere | Staff | Other | None",
  "mentions_competitor": true or false,
  "repeat_customer": true or false,
  "mentions_refund_or_recovery": true or false,
  "suggested_response_tone": "Apologetic | Grateful | Informative | Reassuring",
  "key_points_to_address": "the 1–3 things a reply should address, brief",
  "analysis_summary": "one plain-English sentence capturing the takeaway"
}`;

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ── Supabase helpers (client-agnostic: all config comes from task input) ──────

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

async function fetchUnprocessedReviews(supabaseUrl, serviceKey) {
  // Embedded locations(location_name) replaces the Airtable linked-record lookup.
  const rows = [];
  const PAGE = 1000;
  let start = 0;
  while (true) {
    const batch = await supaFetch(
      supabaseUrl, serviceKey,
      'reviews?processed=eq.false&select=id,review_id,review_text,rating,source_platform,locations(location_name)&order=id.asc',
      { headers: { 'Range-Unit': 'items', Range: `${start}-${start + PAGE - 1}` } },
    );
    rows.push(...(batch || []));
    if (!batch || batch.length < PAGE) break;
    start += PAGE;
  }
  return rows;
}

// ── Claude ────────────────────────────────────────────────────────────────────

async function callClaude(userMessage, apiKey, retryCount = 0) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: 1024,
      system: SYSTEM_PROMPT,
      messages: [{ role: 'user', content: userMessage }],
    }),
  });

  if (res.status === 429 || res.status === 529) {
    if (retryCount >= 5) throw new Error(`Claude API rate limit: gave up after 5 retries`);
    await sleep(Math.pow(2, retryCount) * 2000);
    return callClaude(userMessage, apiKey, retryCount + 1);
  }

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Claude API → ${res.status}: ${text}`);
  }

  const data = await res.json();
  const usage = data.usage || {};
  return {
    text: data.content[0].text,
    inputTokens: usage.input_tokens || 0,
    outputTokens: usage.output_tokens || 0,
  };
}

function parseAnalysis(rawText) {
  let text = rawText.trim();
  text = text.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
  return JSON.parse(text);
}

// ── Main ──────────────────────────────────────────────────────────────────────

Actor.main(async () => {
  const input = await Actor.getInput();
  const { supabaseUrl, supabaseServiceKey, anthropicApiKey, testMode = false } = input;

  if (!supabaseUrl) throw new Error('Missing required input: supabaseUrl');
  if (!supabaseServiceKey) throw new Error('Missing required input: supabaseServiceKey');
  if (!anthropicApiKey) throw new Error('Missing required input: anthropicApiKey');

  console.log(`=== review-analyzer-supabase starting | testMode=${testMode} | model=${MODEL} ===`);

  console.log('Fetching unprocessed reviews...');
  let reviews = await fetchUnprocessedReviews(supabaseUrl, supabaseServiceKey);
  console.log(`Found ${reviews.length} unprocessed reviews.`);

  if (testMode) {
    reviews = reviews.slice(0, 5);
    console.log(`TEST MODE: limiting to first ${reviews.length} reviews.`);
  }

  if (reviews.length === 0) {
    console.log('No unprocessed reviews found. Nothing to do.');
    return;
  }

  let totalProcessed = 0;
  let totalCreated = 0;
  let totalInputTokens = 0;
  let totalOutputTokens = 0;
  const failures = [];

  const BATCH_SIZE = 10;

  for (let i = 0; i < reviews.length; i += BATCH_SIZE) {
    const batch = reviews.slice(i, i + BATCH_SIZE);
    const analysisRows = [];
    const successfulRowIds = [];

    for (const review of batch) {
      const reviewId = review.review_id || '';
      const reviewText = review.review_text || '';
      const rating = review.rating ?? '';
      const platform = review.source_platform || '';
      const locationName = review.locations?.location_name || '';

      const userMessage =
        `Platform: ${platform}\n` +
        `Location: ${locationName}\n` +
        `Star rating: ${rating}\n` +
        `Review: ${reviewText || '(no written text)'}`;

      let analysis;
      try {
        const result = await callClaude(userMessage, anthropicApiKey);
        analysis = parseAnalysis(result.text);
        totalInputTokens += result.inputTokens;
        totalOutputTokens += result.outputTokens;
      } catch (err) {
        console.error(`[SKIP] review_id=${reviewId}: ${err.message}`);
        failures.push({ reviewId, reason: err.message });
        continue;
      }

      if (testMode) {
        console.log('\n────────────────────────────────────────────────────');
        console.log(`review_id  : ${reviewId}`);
        console.log(`review_text: ${reviewText || '(no written text)'}`);
        console.log('Claude JSON:');
        console.log(JSON.stringify(analysis, null, 2));
      }

      analysisRows.push({
        review_id: reviewId,
        sentiment: analysis.sentiment,
        sentiment_score: analysis.sentiment_score,
        urgency: analysis.urgency,
        is_actionable: analysis.is_actionable,
        primary_theme: analysis.primary_theme,
        all_themes: analysis.all_themes,
        food_mentioned: analysis.food_mentioned,
        staff_mentioned: analysis.staff_mentioned,
        staff_sentiment: analysis.staff_sentiment,
        complaint_category: analysis.complaint_category,
        praise_category: analysis.praise_category,
        mentions_competitor: analysis.mentions_competitor,
        repeat_customer: analysis.repeat_customer,
        mentions_refund_or_recovery: analysis.mentions_refund_or_recovery,
        suggested_response_tone: analysis.suggested_response_tone,
        key_points_to_address: analysis.key_points_to_address,
        analysis_summary: analysis.analysis_summary,
        analyzed_at: new Date().toISOString(),
        analysis_model: MODEL,
      });
      successfulRowIds.push(review.id);
    }

    if (analysisRows.length === 0) continue;

    // Write analysis rows (ON CONFLICT DO NOTHING protects against re-runs)
    let writeOk = false;
    try {
      await supaFetch(supabaseUrl, supabaseServiceKey,
        'review_analysis?on_conflict=review_id',
        {
          method: 'POST',
          headers: { Prefer: 'resolution=ignore-duplicates,return=minimal' },
          body: JSON.stringify(analysisRows),
        },
      );
      totalCreated += analysisRows.length;
      writeOk = true;
    } catch (err) {
      console.error(`[ERROR] Supabase write failed (batch at index ${i}): ${err.message}`);
      for (const row of analysisRows) {
        failures.push({ reviewId: row.review_id, reason: `Supabase write failed: ${err.message}` });
      }
    }

    if (!writeOk) continue;

    // Mark source reviews processed = true (only after successful analysis write)
    try {
      await supaFetch(supabaseUrl, supabaseServiceKey,
        `reviews?id=in.(${successfulRowIds.join(',')})`,
        {
          method: 'PATCH',
          headers: { Prefer: 'return=minimal' },
          body: JSON.stringify({ processed: true }),
        },
      );
      totalProcessed += successfulRowIds.length;
    } catch (err) {
      // Analysis rows written — these reviews may be re-analyzed next run, and
      // ON CONFLICT DO NOTHING makes that harmless (same idempotency as v1).
      console.warn(`[WARN] Failed to mark ${successfulRowIds.length} reviews as processed: ${err.message}`);
    }
  }

  const inputCostUSD = (totalInputTokens / 1_000_000) * 1.00;
  const outputCostUSD = (totalOutputTokens / 1_000_000) * 5.00;
  const totalCostUSD = inputCostUSD + outputCostUSD;

  const report = {
    totalProcessed,
    totalCreated,
    totalFailures: failures.length,
    failures,
    inputTokens: totalInputTokens,
    outputTokens: totalOutputTokens,
    estimatedCostUSD: `$${totalCostUSD.toFixed(4)}`,
  };

  console.log('\n══════════════════════════════════════════════');
  console.log('REPORT');
  console.log(`Reviews marked processed=true : ${totalProcessed}`);
  console.log(`Analysis rows created         : ${totalCreated}`);
  console.log(`Failures / skipped            : ${failures.length}`);
  if (failures.length > 0) {
    console.log('Skipped review_ids:');
    failures.forEach(f => console.log(`  - ${f.reviewId}: ${f.reason}`));
  }
  console.log(`Tokens — input: ${totalInputTokens}, output: ${totalOutputTokens}`);
  console.log(`Estimated cost: ${report.estimatedCostUSD}`);
  console.log('══════════════════════════════════════════════');

  await Actor.setValue('REPORT', report);
});
