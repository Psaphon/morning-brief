/**
 * Morning Brief — Research Worker
 *
 * Cloudflare Worker that proxies Claude API calls for on-demand research.
 * Endpoint: POST /api/research
 * Body: { action: "elaborate"|"research"|"sources", article_ids: number[], question?: string }
 *
 * Secrets (set via `wrangler secret put`):
 *   ANTHROPIC_API_KEY
 */

const ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages';
const ANTHROPIC_MODEL = 'claude-sonnet-4-6';
const ANTHROPIC_VERSION = '2023-06-01';
const MAX_TOKENS = 1024;

// Rate limiting: max 10 requests per hour per IP
const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_MS = 60 * 60 * 1000; // 1 hour

const VALID_ACTIONS = new Set(['elaborate', 'research', 'sources']);

// In-memory rate limit store (resets on worker restart; good enough for abuse prevention)
const rateLimitStore = new Map();

/**
 * Returns the number of requests this IP has made in the current window.
 * Increments the counter and returns the new value.
 */
function checkRateLimit(ip) {
  const now = Date.now();
  const entry = rateLimitStore.get(ip);

  if (!entry || now - entry.windowStart > RATE_LIMIT_WINDOW_MS) {
    rateLimitStore.set(ip, { count: 1, windowStart: now });
    return { allowed: true, remaining: RATE_LIMIT_MAX - 1 };
  }

  if (entry.count >= RATE_LIMIT_MAX) {
    const resetAt = new Date(entry.windowStart + RATE_LIMIT_WINDOW_MS);
    return { allowed: false, resetAt };
  }

  entry.count += 1;
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
 * Returns { valid: true, body } or { valid: false, error }.
 */
function validateBody(body) {
  if (!body || typeof body !== 'object') {
    return { valid: false, error: 'Request body must be a JSON object' };
  }

  const { action, article_ids, question } = body;

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

  return { valid: true };
}

/**
 * Look up article data embedded in the request.
 * The dashboard passes article objects along with the request so the worker
 * doesn't need its own database access.
 */
function resolveArticles(articleIds, articlesById) {
  return articleIds
    .map((id) => articlesById[String(id)])
    .filter(Boolean);
}

/**
 * Stream a Claude API response back to the client using Server-Sent Events.
 */
async function streamClaudeResponse(prompt, apiKey, controller) {
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
        model: ANTHROPIC_MODEL,
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
    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: corsHeaders(),
      });
    }

    // Only accept POST /api/research
    const url = new URL(request.url);
    if (url.pathname !== '/api/research') {
      return jsonError(404, 'Not found');
    }
    if (request.method !== 'POST') {
      return jsonError(405, 'Method not allowed');
    }

    // Rate limiting
    const ip = request.headers.get('CF-Connecting-IP') || '0.0.0.0';
    const rateCheck = checkRateLimit(ip);
    if (!rateCheck.allowed) {
      return jsonError(
        429,
        `Rate limit exceeded. Resets at ${rateCheck.resetAt.toISOString()}.`,
        {
          'Retry-After': String(Math.ceil((rateCheck.resetAt - Date.now()) / 1000)),
        }
      );
    }

    // Parse body
    let body;
    try {
      body = await request.json();
    } catch {
      return jsonError(400, 'Invalid JSON body');
    }

    // Validate
    const validation = validateBody(body);
    if (!validation.valid) {
      return jsonError(400, validation.error);
    }

    const { action, article_ids, question, articles_by_id = {} } = body;

    // Check API key is configured
    if (!env.ANTHROPIC_API_KEY) {
      return jsonError(503, 'Research feature not configured (missing API key)');
    }

    // Resolve articles from provided lookup map
    const articles = resolveArticles(article_ids, articles_by_id);
    if (articles.length === 0) {
      return jsonError(400, 'No articles found for the provided article_ids');
    }

    // Build prompt
    let prompt;
    try {
      prompt = buildPrompt(action, articles, question);
    } catch (err) {
      return jsonError(400, err.message);
    }

    // Stream SSE response
    const stream = new ReadableStream({
      async start(controller) {
        await streamClaudeResponse(prompt, env.ANTHROPIC_API_KEY, controller);
      },
    });

    return new Response(stream, {
      status: 200,
      headers: {
        ...corsHeaders(),
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-RateLimit-Remaining': String(rateCheck.remaining ?? 0),
      },
    });
  },
};

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

function jsonError(status, message, extraHeaders = {}) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: {
      ...corsHeaders(),
      'Content-Type': 'application/json',
      ...extraHeaders,
    },
  });
}
