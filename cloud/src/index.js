/**
 * assetlog on Cloudflare Workers (free plan): the phone's copy of the PC app, behind Cloudflare Access.
 *
 * The PC keeps the ledger and the broker keys. It pushes what the read APIs answer (PUT /pc/state) and pulls the
 * phone's writes, which wait here as ops (GET /pc/ops, POST /pc/ops/<id>), and applies them through the real app.
 * Every request, static files included, needs a valid Cf-Access-Jwt-Assertion, and fails closed (503) while
 * ACCESS_TEAM or ACCESS_AUD is unset; the only exception is DEV=1 on a local host (`npm run dev`).
 *
 * Measured, and why the storage looks the way it does:
 * - D1's binding ships a BLOB, both ways, as a JSON array of numbers (miniflare's D1 worker shows the same): turning
 *   1.9 MB into one took ~120 ms of V8 time on the PC and back ~70 ms, against the free plan's 10 ms of CPU per
 *   request. So an op's body is stored as base64 text (the column's BLOB affinity keeps it as given, and node:buffer
 *   encodes natively), which caps a body at MAX_BODY to fit D1's 2,000,000-byte row.
 * - The PC pushes transactions one month per key (transactions-YYYY-MM): as one key, the 253 KB list cost 3-9 ms of CPU
 *   per PUT (measured 2026-09-26) and grew with the ledger; the biggest month is 43 KB. Reads run in D1 (json_each
 *   over every month in one query), so the Worker never parses them. The old single "transactions" key is neither
 *   read nor taken.
 * - PUT /pc/state/<key> (one key per request) stores the pushed text as-is and checks it with SQL's json_valid() in
 *   the same statement: parsing it here, then handing D1 the object again, would cost CPU that grows with the ledger,
 *   the same problem as the op body above, and so would sending it to D1 twice.
 *   That gives up the old whole-push atomicity (one D1 batch for six keys); a push that fails partway leaves the
 *   Worker with a mix of old and new keys until the next run.
 */
import { Buffer } from 'node:buffer'

const SECURITY_HEADERS = {  // the same as web.py's
  'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; " +
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; " +
    "font-src 'self' https://cdn.jsdelivr.net; connect-src 'self' https://cdn.jsdelivr.net; " +
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
}
const MANIFEST = {  // web.py's manifest()
  name: '개미장부', short_name: '개미장부', start_url: '/', display: 'standalone',
  background_color: '#f2f4f6', theme_color: '#f2f4f6',
  icons: [{ src: '/icon.svg', sizes: 'any', type: 'image/svg+xml' }],
}
const MAX_PARTS = 600         // parts of one /api/inbox/part upload: 600 MB at the page's 1 MB a part
const MAX_BODY = 1_400_000   // bytes of an op's body; base64 makes it 1,866,668 of the row's 2,000,000
const MAX_STATE = 1_900_000  // bytes of one pushed item's JSON
const PULL_BUDGET = 2_000_000  // base64 bytes per GET /pc/ops answer: at most one big body, so parsing stays small
const READS = { '/api/summary': 'summary', '/api/accounts': 'accounts', '/api/holdings': 'holdings',
                '/api/import/formats': 'formats' }
const STATE_KEYS = new Set([...Object.values(READS), 'snapshots'])
const MONTH = /^transactions-\d{4}-\d{2}$/  // one month of transactions, the only other state key
const MONTHS = ['transactions-0000-00', 'transactions-9999-99']  // the key range holding every MONTH key, and no other
// the writes web.py has, as "METHOD /path"
const WRITE = /^(POST \/api\/(transactions|snapshots|accounts|import|sync|inbox\/part|gold)|(PATCH|DELETE) \/api\/transactions\/\d+|DELETE \/api\/snapshots)$/
const LOCAL = new Set(['localhost', '127.0.0.1', '[::1]'])
const RS256 = { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }

export default {
  async fetch(request, env) {
    let res
    try {
      res = await route(request, env)
    } catch (e) {
      console.error(e)  // shows in `wrangler tail`; the client gets no stack
      res = json({ detail: 'internal error' }, 500)
    }
    res = new Response(res.body, res)  // asset responses have immutable headers
    for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.headers.set(k, v)
    return res
  },
}

async function route(request, env) {
  const url = new URL(request.url), path = url.pathname
  const denied = await access(request, env, url)
  if (denied) return denied
  if (path.startsWith('/pc/')) return pc(request, env, url)
  if (path.startsWith('/api/')) return api(request, env, url)
  if (path === '/manifest.webmanifest') return json(MANIFEST, 200, 'application/manifest+json')
  return env.ASSETS.fetch(request)
}

// ---- Cloudflare Access

async function access(request, env, url) {
  // DEV=1 is set only by `npm run dev` (wrangler dev --var DEV:1), and counts only on a local host. There JWKS_URL,
  // pointed at a test key set, turns the check back on (test/smoke.mjs).
  const dev = env.DEV === '1' && LOCAL.has(url.hostname)
  if (dev && !env.JWKS_URL) return null
  if (!/^[a-z0-9-]+$/i.test(env.ACCESS_TEAM || '') || !env.ACCESS_AUD) {
    return json({ detail: 'Cloudflare Access is not configured' }, 503)
  }
  const certs = dev ? env.JWKS_URL : `https://${env.ACCESS_TEAM}.cloudflareaccess.com/cdn-cgi/access/certs`
  // 403, not 401: the page reloads on a 401 to reach Access's login, which would loop if Access is misconfigured
  return await verified(request.headers.get('cf-access-jwt-assertion'), env, certs)
    ? null : json({ detail: 'Cloudflare Access token missing or invalid' }, 403)
}

async function verified(token, env, certs) {
  const parts = (token || '').split('.')
  if (parts.length !== 3) return false
  const [h, p, s] = parts
  let head, claims
  try {
    head = JSON.parse(Buffer.from(h, 'base64url').toString())
    claims = JSON.parse(Buffer.from(p, 'base64url').toString())
  } catch {
    return false
  }
  const now = Date.now() / 1000
  // the claims first: a token that can't pass never costs a JWKS fetch
  if (head?.alg !== 'RS256' || claims?.iss !== `https://${env.ACCESS_TEAM}.cloudflareaccess.com`
      || ![].concat(claims.aud).includes(env.ACCESS_AUD) || !(claims.exp > now) || claims.nbf > now + 60) return false
  const key = await accessKey(certs, head.kid)
  return !!key && crypto.subtle.verify(RS256, key, Buffer.from(s, 'base64url'), new TextEncoder().encode(`${h}.${p}`))
}

let jwks = { url: '', at: 0, keys: new Map() }  // per isolate

async function accessKey(url, kid) {
  // Access rotates its signing keys: an unknown kid refetches, at most once a minute
  if (jwks.url !== url || !jwks.keys.has(kid) && Date.now() - jwks.at > 60_000) {
    const r = await fetch(url)
    if (!r.ok) throw new Error(`Access certs: HTTP ${r.status}`)
    const keys = new Map()
    for (const k of (await r.json()).keys ?? []) {
      if (k.kty === 'RSA' && k.kid) keys.set(k.kid, await crypto.subtle.importKey('jwk', k, RS256, false, ['verify']))
    }
    jwks = { url, at: Date.now(), keys }
  }
  return jwks.keys.get(kid)
}

// ---- /api/*: the page's API, reads from what the PC pushed, writes queued

async function api(request, env, url) {
  const { method } = request, path = url.pathname, q = url.searchParams
  if (method !== 'GET' && !sameOrigin(request, url)) return json({ detail: 'cross-origin request refused' }, 403)
  if (path === '/api/inbox') {
    return json({ detail: '클라우드에서는 파일을 조각으로 보내요(/api/inbox/part). 페이지를 새로 고쳐 주세요' }, 400)
  }
  // a file too big for one op arrives as parts the PC puts back together (web.py inbox_part); each part is an op
  if (path === '/api/inbox/part' && !(/^[0-9a-f]{16}$/.test(q.get('upload') || '') && +q.get('n') >= 1
      && +q.get('n') <= MAX_PARTS && /^\d+$/.test(q.get('i') || '') && +q.get('i') >= 0 && +q.get('i') < +q.get('n'))) {
    return json({ detail: 'upload, i, n: a 16-hex id and 0 <= i < n <= ' + MAX_PARTS }, 400)
  }
  if (method === 'GET') {
    if (path === '/api/meta') return meta(env)
    if (READS[path]) return stored(env, READS[path])  // /api/holdings ignores ?refresh: the PC's values are all there is
    if (path === '/api/transactions') return list(env, MONTHS, q.get('kind'), q)
    if (path === '/api/snapshots') return list(env, ['snapshots', 'snapshots'], null, q)
  } else if (WRITE.test(`${method} ${path}`)) {
    return queue(request, env, url)
  }
  return json({ detail: 'Not Found' }, 404)
}

function sameOrigin(request, url) {
  try {
    return new URL(request.headers.get('origin')).host === url.host
  } catch {
    return false  // no Origin, or "null"
  }
}

const notPushed = () => json({ detail: '아직 PC에서 올라온 데이터가 없어요' }, 503)

async function stored(env, key) {
  const row = await env.DB.prepare('SELECT json FROM state WHERE key = ?').bind(key).first()
  return row ? new Response(row.json, { headers: { 'Content-Type': 'application/json' } }) : notPushed()
}

// The PC pushes each list sorted as web.py sorts it (transactions by date DESC, id DESC, split by month): the newest
// key first, then json_each's array index, gives that order back. The key range is a search on the primary key, and
// the sort runs per key, so LIMIT stops reading months once it has enough.
const LIST = `SELECT j.value AS v FROM state s, json_each(s.json) j WHERE s.key BETWEEN ?1 AND ?2
  AND (?3 IS NULL OR json_extract(j.value, '$.kind') = ?3) AND (?4 IS NULL OR json_extract(j.value, '$.account_id') = ?4)
  ORDER BY s.key DESC, j.key LIMIT ?5`

async function list(env, [lo, hi], kind, q) {
  const limit = Math.min(Math.max(parseInt(q.get('limit'), 10) || 200, 1), 200)  // the page never asks for more
  const { results } = await env.DB.prepare(LIST).bind(lo, hi, kind || null, q.get('account_id') || null, limit).all()
  if (!results.length && !await env.DB.prepare('SELECT 1 FROM state WHERE key BETWEEN ? AND ?').bind(lo, hi).first()) {
    return notPushed()
  }
  return new Response(`[${results.map(r => r.v).join(',')}]`, { headers: { 'Content-Type': 'application/json' } })
}

async function meta(env) {
  // `recent`'s last-10 window can bury an old error under newer done ops (the 검토 tab's reclassifies queue fast):
  // `errors` is the last 10 by status alone, so the page's show-once filter never misses one.
  const [head, recent, errors] = await env.DB.batch([
    env.DB.prepare(`SELECT (SELECT updated_at FROM state WHERE key = 'summary') AS pushed_at,
                           (SELECT count(*) FROM ops WHERE status = 'queued') AS pending`),
    env.DB.prepare('SELECT id, at, method, path, status, result FROM ops ORDER BY id DESC LIMIT 10'),
    env.DB.prepare("SELECT id, at, method, path, status, result FROM ops WHERE status = 'error' ORDER BY id DESC LIMIT 10"),
  ])
  return json({ mode: 'cloud', ...head.results[0], recent: recent.results, errors: errors.results })
}

async function queue(request, env, url) {
  const tooBig = () => json({ detail: '파일이 커서 클라우드로는 못 올려요. PC에서 올려 주세요' }, 413)
  if (Number(request.headers.get('content-length')) > MAX_BODY) return tooBig()
  // ponytail: a chunked body is read whole before its size check; only an Access user can send one
  const body = await request.arrayBuffer()
  if (body.byteLength > MAX_BODY) return tooBig()
  if (request.method === 'POST' && url.pathname === '/api/sync') {
    // a broker sync is slow and hits every API; a second tap while one still waits should not queue a second run
    const dup = await env.DB.prepare(
      "SELECT id FROM ops WHERE status = 'queued' AND method = 'POST' AND path = '/api/sync'").first()
    if (dup) return json({ queued: dup.id, detail: 'PC가 켜지면 반영돼요' }, 202)
  }
  const { meta } = await env.DB.prepare(
    'INSERT INTO ops (at, method, path, query, content_type, filename, body) VALUES (?, ?, ?, ?, ?, ?, ?)',
  ).bind(new Date().toISOString(), request.method, url.pathname, url.search.slice(1) || null,
    request.headers.get('content-type'), request.headers.get('x-filename'),  // X-Filename stays URL-encoded
    body.byteLength ? Buffer.from(body).toString('base64') : null).run()
  return json({ queued: meta.last_row_id, detail: 'PC가 켜지면 반영돼요' }, 202)
}

// ---- /pc/*: the PC's side, with X-Assetlog-Key (and, in production, an Access service token)

async function pc(request, env, url) {
  if (!env.PC_KEY) return json({ detail: 'PC_KEY is not set: wrangler secret put PC_KEY' }, 503)
  if (!await sameSecret(request.headers.get('x-assetlog-key') || '', env.PC_KEY)) {
    return json({ detail: 'X-Assetlog-Key missing or wrong' }, 403)
  }
  const { method } = request, path = url.pathname
  const id = path.match(/^\/pc\/ops\/(\d{1,15})$/)
  const key = path.match(/^\/pc\/state\/([a-z0-9-]+)$/)
  if (method === 'GET' && path === '/pc/ops') return pull(env, url.searchParams.get('after') ?? '0')
  if (method === 'POST' && id) return finish(env, Number(id[1]), await request.json().catch(() => null))
  if (method === 'PUT' && key) {
    return STATE_KEYS.has(key[1]) || MONTH.test(key[1])
      ? push(env, key[1], request) : json({ detail: `unknown state key ${key[1]}` }, 400)
  }
  return json({ detail: 'Not Found' }, 404)
}

async function sameSecret(given, want) {  // digests first: timingSafeEqual needs equal lengths
  const enc = new TextEncoder()
  const [a, b] = await Promise.all([given, want].map(s => crypto.subtle.digest('SHA-256', enc.encode(s))))
  return crypto.subtle.timingSafeEqual(a, b)
}

// oldest first, 20 at most, and PULL_BUDGET of bodies in all (the first body always comes, whatever its size)
const PULL = `SELECT id, at, method, path, query, content_type, filename, body FROM (
    SELECT *, COALESCE(length(body), 0) AS size, SUM(COALESCE(length(body), 0)) OVER (ORDER BY id) AS run
    FROM ops WHERE status = 'queued' AND id > ?1 ORDER BY id LIMIT 20)
  WHERE run <= ?2 OR run = size ORDER BY id`

async function pull(env, after) {
  if (!/^\d{1,15}$/.test(after)) return json({ detail: 'after must be an op id' }, 400)
  const { results } = await env.DB.prepare(PULL).bind(Number(after), PULL_BUDGET).all()
  return json({ ops: results.map(({ body, ...op }) => ({ ...op, body_b64: body ?? '' })) })
}

async function finish(env, id, data) {
  if (!['done', 'error'].includes(data?.status)) return json({ detail: 'status must be "done" or "error"' }, 400)
  const r = data.result
  const result = r == null ? null : (typeof r === 'string' ? r : JSON.stringify(r)).slice(0, 1000)
  // the body is dropped once applied: it was only kept for the PC, and D1's 5 GB would fill with old uploads
  const { meta } = await env.DB.prepare('UPDATE ops SET status = ?, result = ?, done_at = ?, body = NULL WHERE id = ?')
    .bind(data.status, result, new Date().toISOString(), id).run()
  return meta.changes ? json({ id, status: data.status }) : json({ detail: `no op ${id}` }, 404)
}

// no row (meta.changes 0) when the text is not JSON; the WHERE also keeps SQLite from reading ON as a join's
const UPSERT = `INSERT INTO state (key, json, updated_at) SELECT ?1, ?2, ?3 WHERE json_valid(?2)
  ON CONFLICT (key) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at`

// One state key per PUT, body text stored as given (never JSON.parse'd): see the top comment for why.
async function push(env, key, request) {
  const pushedAt = request.headers.get('x-pushed-at')
  if (typeof pushedAt !== 'string' || isNaN(Date.parse(pushedAt))) {
    return json({ detail: 'X-Pushed-At must be a date the Worker can parse' }, 400)
  }
  const tooBig = () => json({ detail: `${key} is over ${MAX_STATE} bytes` }, 413)
  if (Number(request.headers.get('content-length')) > MAX_STATE) return tooBig()
  const text = await request.text()
  if (Buffer.byteLength(text) > MAX_STATE) return tooBig()
  const { meta } = await env.DB.prepare(UPSERT).bind(key, text, pushedAt).run()
  if (!meta.changes) return json({ detail: `${key} is not valid JSON` }, 400)
  return json({ saved: key, pushed_at: pushedAt })
}

function json(data, status = 200, type = 'application/json') {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': type } })
}
