/**
 * Morning Brief — Research Worker
 *
 * Cloudflare Worker that proxies Claude API calls for on-demand research.
 * Endpoint: POST /api/research
 * Body: { action: "elaborate"|"research"|"sources", article_ids: number[],
 *         articles_by_id: Record<string, Article>, question?: string }
 *
 * Secrets (set via `wrangler secret put`):
 *   ANTHROPIC_API_KEY     — Anthropic Claude API key
 *   DASHBOARD_HMAC_KEY    — 32-byte hex key shared with the pipeline
 *
 * Vars (wrangler.toml [vars]):
 *   DASHBOARD_ORIGIN      — Allowed CORS origin (e.g. https://brief.example.pages.dev)
 *   ANTHROPIC_MODEL       — Claude model ID (default: claude-sonnet-4-6)
 *
 * KV bindings (wrangler.toml [[kv_namespaces]]):
 *   RATE_LIMIT            — Workers KV namespace for per-IP rate limiting
 */

import { verifyBriefToken, verifyArticleSig } from './auth.js';

const ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages';
const ANTHROPIC_VERSION = '2023-06-01';
const MAX_TOKENS = 1024;
const REQUEST_SIZE_LIMIT = 64 * 1024; // 64 KB

// Rate limiting: max 10 requests per hour per IP
const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_S = 60 * 60; // 1 hour in seconds

const VALID_ACTIONS = new Set(['elaborate', 'research', 'sources']);

/**
 * Returns CORS headers when the request Origin matches env.DASHBOARD_ORIGIN.
 * Omits Access-Control-Allow-Origin entirely for non-matching origins so
 * browsers enforce the same-origin policy.
 */
function corsHeaders(requestOrigin, env) {
  const allowed = env.DASHBOARD_ORIGIN || '';
  if (allowed && requestOrigin === allowed) {
    return {
      'Access-Control-Allow-Origin': allowed,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Dashboard-Token',
    };
  }
  // No CORS header — browsers will block cross-origin requests
  return {};
}

/**
 * Check and increment the per-IP rate limit using Workers KV.
 *
 * KV eventual consistency note: propagation takes ~60 s globally, so a
 * coordinated multi-region attacker may exceed the limit by a small factor
 * during that window. Anthropic account-level spend caps are the hard ceiling.
 */
async function checkRateLimit(ip, env) {
  const kv = env.RATE_LIMIT;
  if (!kv) {
    // KV not bound (dev/test) — allow all requests
    return { allowed: true, remaining: RATE_LIMIT_MAX - 1 };
  }

  const nowS = Math.floor(Date.now() / 1000);
  let entry = await kv.get(ip, 'json');

  if (!entry || nowS - entry.windowStart > RATE_LIMIT_WINDOW_S) {
    entry = { count: 1, windowStart: nowS };
    await kv.put(ip, JSON.stringify(entry), { expirationTtl: RATE_LIMIT_WINDOW_S });
    return { allowed: true, remaining: RATE_LIMIT_MAX - 1 };
  }

  if (entry.count >= RATE_LIMIT_MAX) {
    const resetAt = new Date((entry.windowStart + RATE_LIMIT_WINDOW_S) * 1000);
    return { allowed: false, resetAt };
  }

  entry.count += 1;
  await kv.put(ip, JSON.stringify(entry), { expirationTtl: RATE_LIMIT_WINDOW_S });
  return { allowed: true, remaining: RATE_LIMIT_MAX - entry.count };
}

/**
 * Build the Claude prompt for each action type.
 */
function buildPrompt(action, articles, question) {
  const articleContext = articles
    .map((a, i) => {
      const parts = [`[${i + 1}] ${a.title}`];
      if (a.source) parts.push(`Source: ${a.source}`);
      if (a.summary) parts.push(`Summary: ${a.summary}`);
      return parts.join('\n');
    })
    .join('\n\n');

  switch (action) {
    case 'elaborate':
      return (
        'You are a knowledgeable analyst. Based on the following news articles, explain ' +
        'the broader significance and importance of this topic. Why does it matter? ' +
        'What are the key implications?\n\n' +
        'Articles:\n' +
        articleContext +
        '\n\nProvide a concise, insightful analysis in 2-3 paragraphs.'
      );

    case 'research':
      return (
        'You are a research assistant. Based on the following news articles, investigate ' +
        'this specific question: ' +
        (question || 'What are the key details and context?') +
        '\n\nArticles:\n' +
        articleContext +
        '\n\nAnswer the question thoroughly based on the articles provided. ' +
        'Note any gaps or limitations in the available information.'
      );

    case 'sources':
      return (
        'You are a research librarian. Based on the following news articles, extract ' +
        'and list relevant citations, sources, and further reading suggestions. ' +
        'Include any organizations, reports, studies, or publications mentioned.\n\n' +
        'Articles:\n' +
        articleContext +
        '\n\nProvide a structured list of:\n' +
        '1. Sources cited in or referenced by these articles\n' +
        '2. Key organizations or institutions mentioned\n' +
        '3. Suggested further reading on this topic'
      );

    default:
      throw new Error(`Unknown action: ${action}`);
  }
}

/**
 * Validate the incoming request body.
 * Returns { valid: true } or { valid: false, error }.
 */
function validateBody(body) {
  if (!body || typeof body !== 'object') {
    return { valid: false, error: 'Request body must be a JSON object' };
  }

  const { action, article_ids, question, articles_by_id } = body;

  if (!action || !VALID_ACTIONS.has(action)) {
    return {
      valid: false,
      error: `"action" must be one of: ${[...VALID_ACTIONS].join(', ')}`,
    };
  }

  if (!Array.isArray(article_ids) || article_ids.length === 0) {
    return { valid: false, error: '"article_ids" must be a non-empty array' };
  }

  if (article_ids.length > 20) {
    return { valid: false, error: '"article_ids" must contain at most 20 items' };
  }

  if (action === 'research' && question !== undefined && typeof question !== 'string') {
    return { valid: false, error: '"question" must be a string' };
  }

  // Per-article size caps
  if (articles_by_id && typeof articles_by_id === 'object') {
    for (const [id, art] of Object.entries(articles_by_id)) {
      if (typeof art !== 'object' || art === null) continue;
      if (art.title !== undefined && String(art.title).length > 256) {
        return { valid: false, error: `Article ${id}: "title" exceeds 256 characters` };
      }
      if (art.source !== undefined && String(art.source).length > 256) {
        return { valid: false, error: `Article ${id}: "source" exceeds 256 characters` };
      }
      if (art.summary !== undefined && String(art.summary).length > 4096) {
        return { valid: false, error: `Article ${id}: "summary" exceeds 4096 characters` };
      }
    }
  }

  return { valid: true };
}

/**
 * Look up and verify article data embedded in the request.
 * Rejects (via thrown error) any article whose HMAC signature does not match.
 *
 * Returns the resolved article array, or an { error, status } sentinel if
 * verification fails.
 */
async function resolveArticles(articleIds, articlesById, env) {
  const resolved = [];
  for (const id of articleIds) {
    const art = articlesById[String(id)];
    if (!art) continue;

    const check = await verifyArticleSig(art, env);
    if (!check.valid) {
      return { sigError: check.reason };
    }
    resolved.push(art);
  }
  return { articles: resolved };
}

/**
 * Stream a Claude API response back to the client using Server-Sent Events.
 */
async function streamClaudeResponse(prompt, apiKey, model, controller) {
  const encoder = new TextEncoder();

  let claudeResponse;
  try {
    claudeResponse = await fetch(ANTHROPIC_API_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': ANTHROPIC_VERSION,
      },
      body: JSON.stringify({
        model,
        max_tokens: MAX_TOKENS,
        stream: true,
        messages: [{ role: 'user', content: prompt }],
      }),
    });
  } catch (err) {
    controller.enqueue(
      encoder.encode(`data: ${JSON.stringify({ type: 'error', error: 'Failed to reach Claude API' })}\n\n`)
    );
    controller.close();
    return;
  }

  if (!claudeResponse.ok) {
    let errMsg = `Claude API error: ${claudeResponse.status}`;
    if (claudeResponse.status === 429) {
      errMsg = 'Claude API rate limit reached. Please try again later.';
    } else if (claudeResponse.status === 401) {
      errMsg = 'Claude API authentication failed. Check ANTHROPIC_API_KEY.';
    }
    controller.enqueue(
      encoder.encode(`data: ${JSON.stringify({ type: 'error', error: errMsg })}\n\n`)
    );
    controller.close();
    return;
  }

  const reader = claudeResponse.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // Keep incomplete last line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') continue;

        let event;
        try {
          event = JSON.parse(data);
        } catch {
          continue;
        }

        if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({ type: 'text', text: event.delta.text })}\n\n`
            )
          );
        } else if (event.type === 'message_stop') {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done' })}\n\n`));
        }
      }
    }
  } catch (err) {
    controller.enqueue(
      encoder.encode(`data: ${JSON.stringify({ type: 'error', error: 'Stream interrupted' })}\n\n`)
    );
  } finally {
    controller.close();
  }
}

/**
 * Main Worker fetch handler.
 */
export default {
  async fetch(request, env) {
    const requestOrigin = request.headers.get('Origin') || '';
    const cors = corsHeaders(requestOrigin, env);

    // CORS preflight — only respond with 204 for matching origin
    if (request.method === 'OPTIONS') {
      if (Object.keys(cors).length === 0) {
        return new Response(null, { status: 403 });
      }
      return new Response(null, { status: 204, headers: cors });
    }

    // Only accept POST /api/research
    const url = new URL(request.url);
    if (url.pathname !== '/api/research') {
      return jsonError(404, 'Not found', cors);
    }
    if (request.method !== 'POST') {
      return jsonError(405, 'Method not allowed', cors);
    }

    // Request size cap — reject before parsing JSON
    const contentLength = parseInt(request.headers.get('Content-Length') || '0', 10);
    if (contentLength > REQUEST_SIZE_LIMIT) {
      return jsonError(413, 'Request body too large (limit: 64 KB)', cors);
    }

    // HMAC dashboard token verification
    const token = request.headers.get('X-Dashboard-Token') || '';
    const tokenCheck = await verifyBriefToken(token, env);
    if (!tokenCheck.valid) {
      return jsonError(401, `Unauthorized: ${tokenCheck.reason}`, cors);
    }

    // Rate limiting
    const ip = request.headers.get('CF-Connecting-IP') || '0.0.0.0';
    const rateCheck = await checkRateLimit(ip, env);
    if (!rateCheck.allowed) {
      return jsonError(
        429,
        `Rate limit exceeded. Resets at ${rateCheck.resetAt.toISOString()}.`,
        cors,
        { 'Retry-After': String(Math.ceil((rateCheck.resetAt - Date.now()) / 1000)) }
      );
    }

    // Parse body
    let body;
    try {
      body = await request.json();
    } catch {
      return jsonError(400, 'Invalid JSON body', cors);
    }

    // Validate structure + per-article size caps
    const validation = validateBody(body);
    if (!validation.valid) {
      return jsonError(400, validation.error, cors);
    }

    const { action, article_ids, question, articles_by_id = {} } = body;

    // Check API key is configured
    if (!env.ANTHROPIC_API_KEY) {
      return jsonError(503, 'Research feature not configured (missing API key)', cors);
    }

    // Resolve articles and verify per-article HMAC signatures
    const resolved = await resolveArticles(article_ids, articles_by_id, env);
    if (resolved.sigError) {
      return jsonError(400, `Article signature invalid: ${resolved.sigError}`, cors);
    }
    const articles = resolved.articles;
    if (articles.length === 0) {
      return jsonError(400, 'No articles found for the provided article_ids', cors);
    }

    // Build prompt
    let prompt;
    try {
      prompt = buildPrompt(action, articles, question);
    } catch (err) {
      return jsonError(400, err.message, cors);
    }

    // Env-configurable model with fallback
    const model = env.ANTHROPIC_MODEL || 'claude-sonnet-4-6';

    // Stream SSE response
    const stream = new ReadableStream({
      async start(controller) {
        await streamClaudeResponse(prompt, env.ANTHROPIC_API_KEY, model, controller);
      },
    });

    return new Response(stream, {
      status: 200,
      headers: {
        ...cors,
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-RateLimit-Remaining': String(rateCheck.remaining ?? 0),
      },
    });
  },
};

function jsonError(status, message, corsHdrs = {}, extraHeaders = {}) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: {
      ...corsHdrs,
      'Content-Type': 'application/json',
      ...extraHeaders,
    },
  });
}
