/**
 * HMAC authentication helpers for Morning Brief Worker.
 *
 * Used for:
 *  1. Dashboard token verification — proves the request originates from a
 *     pipeline-generated brief and has not expired (48h window).
 *  2. Per-article signature verification — ensures article content was
 *     produced by the pipeline, preventing the Worker from being used as a
 *     generic Claude proxy with attacker-supplied article text.
 *
 * Token format: "{timestamp_s}:{hmac_hex}"
 * HMAC input:   "{brief_id}:{timestamp_s}" where brief_id is the UTC date
 *               (YYYY-MM-DD) derived from the timestamp.
 *
 * Article sig input: canonical JSON (no spaces) of {id, title, source, summary}.
 */

const TOKEN_MAX_AGE_MS = 48 * 60 * 60 * 1000; // 48 hours

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.slice(i, i + 2), 16);
  }
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

async function computeHmac(hexKey, message) {
  const keyBytes = hexToBytes(hexKey);
  const key = await crypto.subtle.importKey(
    'raw',
    keyBytes,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const msgBytes = new TextEncoder().encode(message);
  const sig = await crypto.subtle.sign('HMAC', key, msgBytes);
  return bytesToHex(new Uint8Array(sig));
}

/** Constant-time hex string comparison to prevent timing attacks. */
function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function briefIdFromTimestamp(timestampS) {
  return new Date(timestampS * 1000).toISOString().slice(0, 10);
}

/**
 * Verify a dashboard brief token.
 *
 * Returns { valid: true } or { valid: false, reason: string }.
 * If DASHBOARD_HMAC_KEY is not configured, allows all requests through (dev mode).
 */
export async function verifyBriefToken(token, env) {
  const hmacKey = env.DASHBOARD_HMAC_KEY;
  if (!hmacKey) {
    return { valid: true };
  }
  if (!token) {
    return { valid: false, reason: 'Missing X-Dashboard-Token' };
  }

  const colonIdx = token.indexOf(':');
  if (colonIdx < 1) {
    return { valid: false, reason: 'Malformed token' };
  }

  const timestampStr = token.slice(0, colonIdx);
  const providedHmac = token.slice(colonIdx + 1);
  const timestampS = parseInt(timestampStr, 10);
  if (isNaN(timestampS)) {
    return { valid: false, reason: 'Malformed token timestamp' };
  }

  const ageMs = Date.now() - timestampS * 1000;
  if (ageMs > TOKEN_MAX_AGE_MS) {
    return { valid: false, reason: 'Token expired (older than 48h)' };
  }
  if (ageMs < -60_000) {
    return { valid: false, reason: 'Token timestamp is in the future' };
  }

  const briefId = briefIdFromTimestamp(timestampS);
  const expectedHmac = await computeHmac(hmacKey, `${briefId}:${timestampStr}`);

  if (!safeEqual(providedHmac, expectedHmac)) {
    return { valid: false, reason: 'Invalid token signature' };
  }

  return { valid: true };
}

/**
 * Verify a per-article HMAC signature.
 *
 * The HMAC covers the canonical JSON of {id, title, source, summary} (no spaces,
 * keys in this exact insertion order — matching the Python pipeline output).
 *
 * Returns { valid: true } or { valid: false, reason: string }.
 * If DASHBOARD_HMAC_KEY is not configured, allows all articles through (dev mode).
 */
export async function verifyArticleSig(article, env) {
  const hmacKey = env.DASHBOARD_HMAC_KEY;
  if (!hmacKey) {
    return { valid: true };
  }

  const { id, title, source, summary, sig } = article;
  if (!sig) {
    return { valid: false, reason: `Article ${id} missing sig` };
  }

  const canonical = JSON.stringify({
    id,
    title: title || '',
    source: source || '',
    summary: summary || '',
  });
  const expectedSig = await computeHmac(hmacKey, canonical);

  if (!safeEqual(sig, expectedSig)) {
    return { valid: false, reason: `Article ${id} signature mismatch` };
  }

  return { valid: true };
}
