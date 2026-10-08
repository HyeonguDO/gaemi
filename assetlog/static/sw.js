// Offline fallback: when the server can't be reached, the phone shows the last data it saw.
// The page and the READS go to the network first; every good answer is also saved with its time. /api/meta exists only
// in cloud mode (cloud/'s Worker): the PC app answers 404, which is never saved.
// Writes (POST/PATCH/DELETE) and the other tabs' reads are never cached: they need the server anyway.
// "Can't be reached" includes a proxy or Cloudflare answering for a server that is down (502-504, 520-530).
// A sign-in answer (401, or Cloudflare Access's redirect: the page fetches with redirect 'manual') is passed through
// and wipes every saved copy, as does logout. Offline, the last copy still shows even if the Access session expired
// since: only the server can say so (keeping it readable offline is this file's purpose).
const CACHE = 'assetlog-v1'
const READS = { '/api/summary': 5000, '/api/accounts': 5000, '/api/holdings': 30000, '/api/meta': 5000 }  // ms to wait; broker reads take ~10 s
const PAGE_WAIT = 5000
const DOWN_WAIT = 1500  // after one miss, wait only this long for 30 s: a server that is back still answers in time
let downUntil = 0

const unreachable = s => s === 502 || s === 503 || s === 504 || (s >= 520 && s <= 530)
const signIn = r => r.status === 401 || r.type === 'opaqueredirect'
// A read still in flight keeps its handle on the deleted cache, so what it saves afterwards is discarded.
const clearAll = async () => Promise.all((await caches.keys()).map(k => caches.delete(k)))

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))
self.addEventListener('message', e => { if (e.data?.type === 'logout') e.waitUntil(clearAll()) })

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url)
  if (req.method !== 'GET') return
  if (url.hostname === 'cdn.jsdelivr.net') return e.respondWith(cacheFirst(req))  // Chart.js, Pretendard CSS + fonts (pinned)
  if (url.origin !== location.origin) return
  if (req.mode === 'navigate' && url.pathname === '/') return e.respondWith(networkFirst(req, '/', PAGE_WAIT))
  if (url.pathname in READS) e.respondWith(networkFirst(req, url.pathname, READS[url.pathname]))  // ?refresh=1 shares the key
})

async function networkFirst(req, key, wait) {
  const cache = await caches.open(CACHE)
  const net = fetch(req).then(async r => {
    if (signIn(r)) await clearAll()
    else if (r.ok && !r.redirected) {
      const headers = new Headers(r.headers)
      headers.set('X-Saved-At', new Date().toISOString())
      await cache.put(key, new Response(await r.clone().blob(), { status: 200, headers }))
      downUntil = 0
    }
    return r
  })
  const timeout = new Promise(res => setTimeout(res, Date.now() < downUntil ? Math.min(wait, DOWN_WAIT) : wait))
  const r = await Promise.race([net.catch(() => null), timeout])
  if (r && !unreachable(r.status)) return r  // the server answered, errors and sign-in included: show what it said
  downUntil = Date.now() + 30000
  const saved = await cache.match(key)
  if (!saved) return net  // nothing saved yet: let the real result or error through
  net.catch(() => {})     // it may still finish and refresh the cache for next time
  const headers = new Headers(saved.headers)
  headers.set('X-Offline', '1')
  return new Response(saved.body, { status: 200, headers })
}

// No SRI (the floating @4 tag has no fixed hash), so at least keep only a good copy: a cors fetch shows the real
// status, where a no-cors one is opaque and would pin a CDN error page for good.
async function cacheFirst(req) {
  const cache = await caches.open(CACHE), hit = await cache.match(req)
  if (hit?.ok) return hit  // an opaque copy saved by an older version can't answer a cors request: fetch again
  return fetch(req.url, { mode: 'cors', credentials: 'omit' }).then(r => { if (r.ok) cache.put(req, r.clone()); return r })
}
