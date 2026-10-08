// End-to-end check of the Worker under `wrangler dev --local` (npm test). No Cloudflare account, and the Worker calls
// nothing outside: a throwaway local D1, and a JWKS served from here for a locally generated RSA key.
// Runs the Worker four times: DEV=1 (the API), DEV=1 with JWKS_URL (the Access check), and DEV unset with and
// without Access settings (fail closed).
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createSign, generateKeyPairSync, randomBytes } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const WRANGLER = join(ROOT, 'node_modules', '.bin', 'wrangler')
const ENV = { ...process.env, WRANGLER_SEND_METRICS: 'false', NO_COLOR: '1' }
const PERSIST = mkdtempSync(join(tmpdir(), 'assetlog-cloud-'))
const KEY = 'test-pc-key'
const TEAM = 'testteam', AUD = 'testaud'
let passed = 0

function ok(name, fn) {
  return Promise.resolve().then(fn).then(() => { passed++; console.log('ok  ', name) },
    e => { console.error('FAIL', name); throw e })
}

function req(port, method, path, { headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const r = httpRequest({ host: '127.0.0.1', port, method, path, headers }, res => {
      const chunks = []
      res.on('data', c => chunks.push(c))
      res.on('end', () => {
        const buf = Buffer.concat(chunks)
        resolve({ status: res.statusCode, headers: res.headers, text: buf.toString(), json: () => JSON.parse(buf) })
      })
    })
    r.on('error', reject)
    r.end(body)
  })
}

function freePort() {
  return new Promise(resolve => {
    const s = createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)) })
  })
}

async function worker(vars) {
  const port = await freePort()
  // wrangler.toml's real [vars] would otherwise leak in: blank the Access ones a worker doesn't set
  const blank = ['ACCESS_TEAM:', 'ACCESS_AUD:'].filter(b => !vars.some(v => v.startsWith(b)))
  const args = ['dev', '--local', '--persist-to', PERSIST, '--port', String(port), '--ip', '127.0.0.1',
    '--show-interactive-dev-session=false', '--var', ...blank, ...vars]
  const child = spawn(WRANGLER, args, { cwd: ROOT, env: ENV, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let log = ''
  child.stdout.on('data', d => { log += d })
  child.stderr.on('data', d => { log += d })
  for (let i = 0; i < 120; i++) {
    try {
      await req(port, 'GET', '/manifest.webmanifest')
      return { port, stop: () => process.kill(-child.pid), log: () => log }
    } catch {
      await new Promise(r => setTimeout(r, 250))
    }
  }
  process.kill(-child.pid)
  throw new Error(`wrangler dev did not start:\n${log}`)
}

// ---- keys

const good = generateKeyPairSync('rsa', { modulusLength: 2048 })
const other = generateKeyPairSync('rsa', { modulusLength: 2048 })
const b64u = x => Buffer.from(typeof x === 'string' ? x : JSON.stringify(x)).toString('base64url')
const now = () => Math.floor(Date.now() / 1000)
const CLAIMS = () => ({ aud: [AUD], iss: `https://${TEAM}.cloudflareaccess.com`, exp: now() + 600, iat: now(), nbf: now() - 5,
  email: 'me@example.com' })

function jwt(claims, { key = good.privateKey, kid = 'k1', alg = 'RS256' } = {}) {
  const head = b64u({ alg, kid, typ: 'JWT' }), body = b64u(claims)
  return `${head}.${body}.${createSign('RSA-SHA256').update(`${head}.${body}`).sign(key).toString('base64url')}`
}

let certsFetched = 0
const jwks = createServer((q, res) => {
  certsFetched++
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify({ keys: [{ ...good.publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256', use: 'sig' }] }))
})
await new Promise(r => jwks.listen(0, '127.0.0.1', r))
const JWKS_URL = `http://127.0.0.1:${jwks.address().port}/certs`

// ---- data the PC would push

// as web.py answers /api/transactions (date DESC, id DESC); the PC pushes it as one key per month, here three
const TX = Array.from({ length: 250 }, (_, i) => ({
  id: i + 1, account_id: i % 2 ? 'kis' : 'toss', date: `2026-0${7 + (i % 3)}-1${i % 4}`,
  kind: i % 11 ? (i % 5 ? 'DEPOSIT' : 'DIVIDEND') : 'UNKNOWN',
  amount: i % 3 ? 1000.5 + i : -2e-7, memo: `메모 "${i}" \\ ✓`, source: 'manual', kind_locked: 0, raw: i % 7 ? null : '{"a":1}',
})).sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id)
const MONTHS = {}  // transactions-YYYY-MM: rows, newest month first
for (const t of TX) (MONTHS[`transactions-${t.date.slice(0, 7)}`] ??= []).push(t)
const OLD_TX = [{ ...TX[0], id: -1, kind: 'UNKNOWN', memo: 'the old single key' }]  // before months: never read
const SNAPS = [{ account_id: 'kis', date: '2026-09-25', value: 100, cash: null, source: 'kis' },
  { account_id: 'toss', date: '2026-09-25', value: 200, cash: 5, source: 'toss' },
  { account_id: 'kis', date: '2026-08-31', value: 90, cash: 1, source: 'manual' }]
const ITEMS = { summary: { total: 300, note: '총자산' }, accounts: [{ id: 'kis', institution: 'kis', name: '한투', kind: 'brokerage' }],
  holdings: { fetched_at: '2026-09-26T23:50:00+09:00', accounts: [] }, formats: { toss_pdf: '토스 PDF' },
  ...Object.fromEntries(Object.entries(MONTHS).reverse()), snapshots: SNAPS }  // oldest month first: order is the key's
const PUSHED_AT = '2026-09-26T23:50:01+09:00'

const schema = spawnSync(WRANGLER, ['d1', 'execute', 'assetlog', '--local', '--persist-to', PERSIST, '--file', 'schema.sql'],
  { cwd: ROOT, env: ENV, encoding: 'utf8' })
assert.equal(schema.status, 0, `schema.sql: ${schema.stdout}${schema.stderr}`)

const SEC = { 'content-security-policy': "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; " +
  "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; " +
  "font-src 'self' https://cdn.jsdelivr.net; connect-src 'self' https://cdn.jsdelivr.net; " +
  "frame-ancestors 'none'; base-uri 'none'; form-action 'self'", 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' }
const secured = r => { for (const [k, v] of Object.entries(SEC)) assert.equal(r.headers[k], v, k) }

try {
  await ok('schema.sql indexes ops by status (meta, the pull and the sync dedup never scan every op)', () => {
    const r = spawnSync(WRANGLER, ['d1', 'execute', 'assetlog', '--local', '--persist-to', PERSIST, '--json', '--command',
      "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ops_status'"], { cwd: ROOT, env: ENV, encoding: 'utf8' })
    assert.equal(r.status, 0, r.stdout + r.stderr)
    assert.deepEqual(JSON.parse(r.stdout)[0].results, [{ sql: 'CREATE INDEX ops_status ON ops(status, id)' }])
  })
  await ok('an old single "transactions" row, as an earlier version pushed it, sits in D1', () => {
    const r = spawnSync(WRANGLER, ['d1', 'execute', 'assetlog', '--local', '--persist-to', PERSIST, '--command',
      `INSERT INTO state VALUES ('transactions', '${JSON.stringify(OLD_TX)}', '${PUSHED_AT}')`],
    { cwd: ROOT, env: ENV, encoding: 'utf8' })
    assert.equal(r.status, 0, r.stdout + r.stderr)
  })

  // ---- DEV=1: the API
  let w = await worker(['DEV:1', `PC_KEY:${KEY}`])
  const get = (path, headers) => req(w.port, 'GET', path, { headers })
  const origin = { Origin: `http://127.0.0.1:${w.port}` }
  const send = (method, path, body, headers = {}) => req(w.port, method, path,
    { headers: { ...origin, 'Content-Type': 'application/json', ...headers }, body })
  const pc = (method, path, body, key = KEY) => req(w.port, method, path,
    { headers: { 'X-Assetlog-Key': key, 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) })
  const state = (key, body, { pushedAt = PUSHED_AT, keyHeader = KEY } = {}) => req(w.port, 'PUT', `/pc/state/${key}`,
    { headers: { 'X-Assetlog-Key': keyHeader, ...(pushedAt == null ? {} : { 'X-Pushed-At': pushedAt }) },
      body: typeof body === 'string' ? body : JSON.stringify(body) })
  try {
    await ok('static page and sw.js carry the security headers', async () => {
      for (const p of ['/', '/sw.js', '/icon.svg']) { const r = await get(p); assert.equal(r.status, 200, p); secured(r) }
      assert.match((await get('/')).text, /<html/i)
    })
    await ok('manifest is web.py\'s', async () => {
      const r = await get('/manifest.webmanifest')
      assert.equal(r.headers['content-type'], 'application/manifest+json')
      assert.deepEqual(r.json(), { name: '개미장부', short_name: '개미장부', start_url: '/', display: 'standalone',
        background_color: '#f2f4f6', theme_color: '#f2f4f6', icons: [{ src: '/icon.svg', sizes: 'any', type: 'image/svg+xml' }] })
    })
    await ok('reads before any push: 503 with the Korean detail', async () => {
      for (const p of ['/api/summary', '/api/accounts', '/api/holdings', '/api/import/formats', '/api/transactions', '/api/snapshots']) {
        const r = await get(p)
        assert.equal(r.status, 503, p)
        assert.deepEqual(r.json(), { detail: '아직 PC에서 올라온 데이터가 없어요' })
        secured(r)
      }
    })
    await ok('meta before any push', async () => {
      assert.deepEqual((await get('/api/meta')).json(),
        { mode: 'cloud', pushed_at: null, pending: 0, recent: [], errors: [] })
    })
    await ok('DEV=1 counts only on a local host', async () => {
      const r = await req(w.port, 'GET', '/api/meta', { headers: { Host: 'assetlog.example.workers.dev' } })
      assert.equal(r.status, 503)
      assert.deepEqual(r.json(), { detail: 'Cloudflare Access is not configured' })
    })
    await ok('/pc/* needs the key', async () => {
      assert.equal((await req(w.port, 'GET', '/pc/ops')).status, 403)
      assert.equal((await pc('GET', '/pc/ops', null, 'wrong')).status, 403)
      assert.equal((await pc('GET', '/pc/ops', null, KEY + 'x')).status, 403)
      assert.equal((await state('summary', ITEMS.summary, { keyHeader: '' })).status, 403)
      assert.equal((await get('/api/meta')).json().pushed_at, null)
    })
    await ok('PUT /pc/state/<key> rejects bad input', async () => {
      assert.equal((await req(w.port, 'PUT', '/pc/state/summary', { headers: { 'X-Assetlog-Key': KEY }, body: '{}' })).status, 400)  // no X-Pushed-At
      assert.equal((await state('summary', {}, { pushedAt: 'soon' })).status, 400)  // unparseable date
      assert.equal((await state('evil', {})).status, 400)  // not one of the six keys
      for (const key of ['transactions', 'transactions-2026-9', 'transactions-26-09', 'transactions-2026-0a',
        'transactions-2026-09-01', 'transactions--2026-09', 'snapshots-2026-09']) {
        assert.equal((await state(key, [])).status, 400, key)  // the old single key, or not a month's
      }
      assert.equal((await state('Transactions-2026-09', [])).status, 404)
      assert.equal((await state('transactions-2026-09/x', [])).status, 404)
      const bad = await req(w.port, 'PUT', '/pc/state/summary', { headers: { 'X-Assetlog-Key': KEY, 'X-Pushed-At': PUSHED_AT }, body: '{nope' })
      assert.equal(bad.status, 400)
      assert.equal(typeof bad.json().detail, 'string')
      const big = await state('transactions-2026-09', '"' + 'x'.repeat(1_900_000) + '"')  // valid JSON, over MAX_STATE
      assert.equal(big.status, 413)
      assert.equal((await get('/api/summary')).status, 503, 'nothing of a rejected push is kept')
      assert.equal((await get('/api/transactions')).status, 503, 'nor is the old single key read')
    })
    await ok('PUT /pc/state/<key> stores every item', async () => {
      for (const [key, item] of Object.entries(ITEMS)) {
        const r = await state(key, item)
        assert.equal(r.status, 200, key)
        assert.deepEqual(r.json(), { saved: key, pushed_at: PUSHED_AT })
      }
      assert.deepEqual((await get('/api/summary')).json(), ITEMS.summary)
      assert.deepEqual((await get('/api/accounts')).json(), ITEMS.accounts)
      assert.deepEqual((await get('/api/holdings?refresh=1')).json(), ITEMS.holdings)
      assert.deepEqual((await get('/api/import/formats')).json(), ITEMS.formats)
      assert.equal((await get('/api/meta')).json().pushed_at, PUSHED_AT)
    })
    await ok('PUT /pc/state/<key> overwrites, and invalid JSON keeps what is stored', async () => {
      const later = '2026-09-27T00:05:00+09:00', next = { ...ITEMS.summary, total: 301 }
      assert.equal((await state('summary', next, { pushedAt: later })).status, 200)
      const bad = await state('summary', '{nope', { pushedAt: '2026-09-27T00:20:00+09:00' })
      assert.equal(bad.status, 400)
      assert.deepEqual(bad.json(), { detail: 'summary is not valid JSON' })
      assert.deepEqual((await get('/api/summary')).json(), next)
      assert.equal((await get('/api/meta')).json().pushed_at, later)
    })
    await ok('transactions: the months read as one list; default 200, limit, max 200, filters, order and values kept', async () => {
      assert.equal(Object.keys(MONTHS).length, 3)
      assert.ok(TX[199].date < '2026-08', 'the first 200 span every month')
      assert.deepEqual((await get('/api/transactions')).json(), TX.slice(0, 200))
      assert.deepEqual((await get('/api/transactions?limit=5')).json(), TX.slice(0, 5))
      assert.deepEqual((await get('/api/transactions?limit=5000')).json(), TX.slice(0, 200))
      assert.deepEqual((await get('/api/transactions?limit=99999')).json(), TX.slice(0, 200))
      assert.deepEqual((await get('/api/transactions?limit=5000&kind=DIVIDEND')).json(), TX.filter(t => t.kind === 'DIVIDEND'))
      assert.deepEqual((await get('/api/transactions?kind=DEPOSIT&account_id=kis&limit=3')).json(),
        TX.filter(t => t.kind === 'DEPOSIT' && t.account_id === 'kis').slice(0, 3))
      assert.deepEqual((await get('/api/transactions?kind=&account_id=')).json(), TX.slice(0, 200))
      assert.deepEqual((await get('/api/transactions?account_id=nobody')).json(), [])
      for (const k of ['', 'UNKNOWN', 'DEPOSIT', 'DIVIDEND']) {  // the 검토 tab's own calls
        assert.deepEqual((await get('/api/transactions?limit=200' + (k ? '&kind=' + k : ''))).json(),
          TX.filter(t => !k || t.kind === k).slice(0, 200), k)
      }
    })
    await ok('transactions: a month PUT again replaces only that month, and one PUT as [] drops out', async () => {
      const month = Object.keys(MONTHS)[1], inMonth = t => `transactions-${t.date.slice(0, 7)}` === month
      assert.equal((await state(month, MONTHS[month].map(t => ({ ...t, memo: 'edited' })))).status, 200)
      assert.deepEqual((await get('/api/transactions')).json(),
        TX.map(t => inMonth(t) ? { ...t, memo: 'edited' } : t).slice(0, 200))
      assert.equal((await state(month, [])).status, 200)
      assert.deepEqual((await get('/api/transactions')).json(), TX.filter(t => !inMonth(t)).slice(0, 200))
      assert.deepEqual((await get('/api/transactions?kind=UNKNOWN')).json(), TX.filter(t => !inMonth(t) && t.kind === 'UNKNOWN'))
    })
    await ok('snapshots: account filter and limit', async () => {
      assert.deepEqual((await get('/api/snapshots')).json(), SNAPS)
      assert.deepEqual((await get('/api/snapshots?account_id=kis&limit=1')).json(), [SNAPS[0]])
      assert.deepEqual((await get('/api/snapshots?account_id=kis')).json(), [SNAPS[0], SNAPS[2]])
    })

    const file = randomBytes(1000)
    const writes = [
      ['POST', '/api/snapshots', JSON.stringify({ account_id: 'kis', date: '2026-09-26', value: 123 })],
      ['POST', '/api/transactions', JSON.stringify({ account_id: 'kis', date: '2026-09-26', kind: 'DEPOSIT', amount: 5 })],
      ['PATCH', '/api/transactions/12', JSON.stringify({ kind: 'INTERNAL' })],
      ['DELETE', '/api/transactions/12'],
      ['DELETE', '/api/snapshots?account_id=kis&date=2026-08-31'],
      ['POST', '/api/accounts', JSON.stringify({ id: 'x', institution: 'x', name: 'x' })],
      ['POST', '/api/sync'],
    ]
    await ok('writes need a same-host Origin', async () => {
      const body = writes[0][2]
      const h = { 'Content-Type': 'application/json' }
      assert.equal((await req(w.port, 'POST', '/api/snapshots', { headers: h, body })).status, 403)
      assert.equal((await req(w.port, 'POST', '/api/snapshots', { headers: { ...h, Origin: 'https://evil.example' }, body })).status, 403)
      assert.equal((await req(w.port, 'POST', '/api/snapshots', { headers: { ...h, Origin: 'null' }, body })).status, 403)
      assert.equal((await get('/api/meta')).json().pending, 0)
    })
    await ok('writes are queued: 202', async () => {
      let id = 0
      for (const [method, path, body] of writes) {
        const r = await send(method, path, body)
        assert.equal(r.status, 202, `${method} ${path}`)
        assert.deepEqual(r.json(), { queued: ++id, detail: 'PC가 켜지면 반영돼요' })
        secured(r)
      }
      const r = await send('POST', '/api/import?account_id=toss&fmt=toss_pdf', file,
        { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent('토스 거래내역.pdf') })
      assert.equal(r.status, 202)
    })
    await ok('413 for a body over 1.4 MB, 404 for other /api paths, 400 for /api/inbox', async () => {
      const r = await send('POST', '/api/import?account_id=toss&fmt=toss_pdf', Buffer.alloc(1_400_001),
        { 'Content-Type': 'application/octet-stream' })
      assert.equal(r.status, 413)
      assert.deepEqual(r.json(), { detail: '파일이 커서 클라우드로는 못 올려요. PC에서 올려 주세요' })
      for (const [m, p] of [['GET', '/api/nope'], ['PUT', '/api/transactions'], ['POST', '/api/transactions/abc'],
        ['POST', '/api/login'], ['POST', '/api/logout'], ['GET', '/api/transactions/3'], ['POST', '/api/meta'],
        ['PATCH', '/api/snapshots'], ['POST', '/api/import/formats']]) {
        const r = await send(m, p, '{}')
        assert.equal(r.status, 404, `${m} ${p}`)
        assert.deepEqual(r.json(), { detail: 'Not Found' })
      }
      for (const m of ['GET', 'POST']) {
        const r = await send(m, '/api/inbox', m === 'POST' ? 'x' : undefined)
        assert.equal(r.status, 400)
        assert.deepEqual(r.json(), { detail: '클라우드에서는 파일을 조각으로 보내요(/api/inbox/part). 페이지를 새로 고쳐 주세요' })
      }
    })
    await ok('meta counts what waits and shows the last 10, newest first', async () => {
      const m = (await get('/api/meta')).json()
      assert.equal(m.pending, 8)
      assert.deepEqual(m.recent.map(o => o.id), [8, 7, 6, 5, 4, 3, 2, 1])
      assert.deepEqual(Object.keys(m.recent[0]).sort(), ['at', 'id', 'method', 'path', 'result', 'status'])
      assert.equal(m.recent[7].status, 'queued')
    })
    let ops
    await ok('GET /pc/ops gives each op as it came, body in base64', async () => {
      assert.equal((await pc('GET', '/pc/ops?after=x')).status, 400)
      ops = (await pc('GET', '/pc/ops?after=0')).json().ops
      assert.deepEqual(ops.map(o => o.id), [1, 2, 3, 4, 5, 6, 7, 8])
      assert.deepEqual(Object.keys(ops[0]).sort(),
        ['at', 'body_b64', 'content_type', 'filename', 'id', 'method', 'path', 'query'])
      writes.forEach(([method, path, body], i) => {
        const [p, q] = path.split('?')
        assert.equal(ops[i].method, method)
        assert.equal(ops[i].path, p)
        assert.equal(ops[i].query, q ?? null)
        assert.equal(Buffer.from(ops[i].body_b64, 'base64').toString(), body ?? '')
        assert.equal(ops[i].content_type, 'application/json')
      })
      const imp = ops[7]
      assert.equal(imp.query, 'account_id=toss&fmt=toss_pdf')
      assert.equal(imp.content_type, 'application/octet-stream')
      assert.equal(imp.filename, encodeURIComponent('토스 거래내역.pdf'))
      assert.ok(Buffer.from(imp.body_b64, 'base64').equals(file))
      assert.deepEqual((await pc('GET', '/pc/ops?after=6')).json().ops.map(o => o.id), [7, 8])
    })
    await ok('POST /pc/ops/<id> records the outcome', async () => {
      assert.equal((await pc('POST', '/pc/ops/1', { status: 'maybe' })).status, 400)
      assert.equal((await pc('POST', '/pc/ops/999', { status: 'done' })).status, 404)
      assert.deepEqual((await pc('POST', '/pc/ops/1', { status: 'done' })).json(), { id: 1, status: 'done' })
      assert.equal((await pc('POST', '/pc/ops/2', { status: 'error', result: 'e'.repeat(1500) })).status, 200)
      assert.equal((await pc('POST', '/pc/ops/3', { status: 'error', result: [{ msg: 'bad' }] })).status, 200)
      for (let id = 4; id <= 8; id++) assert.equal((await pc('POST', `/pc/ops/${id}`, { status: 'done' })).status, 200)
      const m = (await get('/api/meta')).json()
      assert.equal(m.pending, 0)
      const byId = Object.fromEntries(m.recent.map(o => [o.id, o]))
      assert.equal(byId[2].result, 'e'.repeat(1000))
      assert.equal(byId[3].result, '[{"msg":"bad"}]')
      assert.equal(byId[1].status, 'done')
      assert.deepEqual((await pc('GET', '/pc/ops?after=0')).json().ops, [])
    })
    await ok('duplicate queued POST /api/sync coalesces to the existing op', async () => {
      const a = await send('POST', '/api/sync'), b = await send('POST', '/api/sync')
      assert.equal(a.status, 202); assert.equal(b.status, 202)
      assert.equal(a.json().queued, b.json().queued)
      assert.equal((await get('/api/meta')).json().pending, 1)  // not 2: one op, one broker sync
      await pc('POST', `/pc/ops/${a.json().queued}`, { status: 'done' })  // clean up for the next test
    })
    await ok('one GET /pc/ops carries at most one big body', async () => {
      const big = () => send('POST', '/api/import?account_id=toss&fmt=toss_pdf', randomBytes(1_300_000),
        { 'Content-Type': 'application/octet-stream' })
      const ids = [(await big()).json().queued, (await big()).json().queued, (await send('POST', '/api/sync')).json().queued]
      assert.deepEqual((await pc('GET', '/pc/ops?after=0')).json().ops.map(o => o.id), [ids[0]])
      assert.deepEqual((await pc('GET', `/pc/ops?after=${ids[0]}`)).json().ops.map(o => o.id), [ids[1], ids[2]])
      assert.equal((await pc('GET', `/pc/ops?after=${ids[1]}`)).json().ops[0].body_b64, '')
      for (const id of ids) await pc('POST', `/pc/ops/${id}`, { status: 'done' })  // clean up for the next test
    })
    await ok('meta keeps an old error out of recent but still lists it in errors', async () => {
      const errId = (await send('POST', '/api/sync')).json().queued
      await pc('POST', `/pc/ops/${errId}`, { status: 'error', result: '동기화 실패' })
      for (let i = 0; i < 10; i++) {
        const id = (await send('POST', '/api/sync')).json().queued  // never coalesces: finished before the next queues
        await pc('POST', `/pc/ops/${id}`, { status: 'done' })
      }
      const m = (await get('/api/meta')).json()
      assert.ok(!m.recent.some(o => o.id === errId), 'buried past the last 10')
      assert.ok(m.errors.some(o => o.id === errId), 'still surfaced in errors')
    })
    await ok('other /pc paths answer 404', async () => {
      assert.equal((await pc('GET', '/pc/state')).status, 404)
      assert.equal((await pc('DELETE', '/pc/ops/1')).status, 404)
    })
    await ok('a file part is queued with its query and name; a bad upload id or index is 400', async () => {
      const part = (q, body = 'abc') => send('POST', `/api/inbox/part?${q}`, body,
        { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent('녹화.mp4') })
      for (const q of ['upload=xyz&i=0&n=2', 'upload=0123456789abcdef&i=2&n=2', 'upload=0123456789abcdef&n=2',
        'upload=0123456789abcdef&i=0&n=0', 'upload=0123456789abcdef&i=0&n=601', 'upload=0123456789abcdef&i=-1&n=2']) {
        assert.equal((await part(q)).status, 400, q)
      }
      const r = await part('upload=0123456789abcdef&i=1&n=2')
      assert.equal(r.status, 202)
      const op = (await pc('GET', '/pc/ops?after=8')).json().ops.find(o => o.id === r.json().queued)
      assert.deepEqual([op.method, op.path, op.query, op.filename, Buffer.from(op.body_b64, 'base64').toString()],
        ['POST', '/api/inbox/part', 'upload=0123456789abcdef&i=1&n=2', encodeURIComponent('녹화.mp4'), 'abc'])
      assert.equal((await pc('POST', `/pc/ops/${op.id}`, { status: 'done' })).status, 200)
    })
  } finally {
    w.stop()
  }

  // ---- DEV=1 with JWKS_URL: the Access check against the local key
  w = await worker(['DEV:1', `PC_KEY:${KEY}`, `ACCESS_TEAM:${TEAM}`, `ACCESS_AUD:${AUD}`, `JWKS_URL:${JWKS_URL}`])
  const as = (token, path = '/api/meta', headers = {}) =>
    req(w.port, 'GET', path, { headers: token == null ? headers : { 'Cf-Access-Jwt-Assertion': token, ...headers } })
  try {
    await ok('a valid Access token passes, static files and /pc included', async () => {
      assert.equal((await as(jwt(CLAIMS()))).status, 200)
      assert.equal((await as(jwt({ ...CLAIMS(), aud: ['other', AUD] }), '/')).status, 200)
      assert.equal((await as(jwt({ ...CLAIMS(), aud: AUD }))).status, 200)
      assert.equal((await as(jwt(CLAIMS()), '/pc/ops', { 'X-Assetlog-Key': KEY })).status, 200)
      assert.equal((await as(jwt(CLAIMS()), '/pc/ops')).status, 403, 'Access alone is not enough for /pc')
    })
    const tampered = () => { const [h, , s] = jwt(CLAIMS()).split('.'); return `${h}.${b64u({ ...CLAIMS(), email: 'x@y' })}.${s}` }
    for (const [name, token] of [
      ['no token', null], ['empty', ''], ['garbage', 'a.b.c'], ['two parts', 'a.b'],
      ['bad aud', jwt({ ...CLAIMS(), aud: ['other'] })], ['no aud', jwt({ ...CLAIMS(), aud: undefined })],
      ['bad iss', jwt({ ...CLAIMS(), iss: 'https://evil.cloudflareaccess.com' })],
      ['expired', jwt({ ...CLAIMS(), exp: now() - 1 })], ['no exp', jwt({ ...CLAIMS(), exp: undefined })],
      ['not yet valid', jwt({ ...CLAIMS(), nbf: now() + 3600 })],
      ['other key, same kid', jwt(CLAIMS(), { key: other.privateKey })],
      ['unknown kid', jwt(CLAIMS(), { key: other.privateKey, kid: 'k2' })],
      ['kid "constructor"', jwt(CLAIMS(), { kid: 'constructor' })],
      ['alg HS256', jwt(CLAIMS(), { alg: 'HS256' })], ['alg none', `${b64u({ alg: 'none' })}.${b64u(CLAIMS())}.`],
      ['tampered payload', tampered()], ['payload null', `${b64u({ alg: 'RS256', kid: 'k1' })}.${b64u('null')}.x`],
    ]) {
      await ok(`Access token rejected: ${name}`, async () => {
        for (const p of ['/api/meta', '/', '/manifest.webmanifest']) {
          const r = await as(token, p)
          assert.equal(r.status, 403, p)
          assert.deepEqual(r.json(), { detail: 'Cloudflare Access token missing or invalid' })
          secured(r)
        }
      })
    }
    await ok('the JWKS is fetched once per isolate (an unknown kid refetches at most once a minute)', async () => {
      assert.equal(certsFetched, 1)
    })
  } finally {
    w.stop()
  }

  // ---- DEV unset: fail closed
  w = await worker([`PC_KEY:${KEY}`])
  try {
    await ok('DEV unset, Access not configured: 503 everywhere', async () => {
      for (const p of ['/', '/sw.js', '/api/meta', '/api/summary', '/manifest.webmanifest']) {
        const r = await as(jwt(CLAIMS()), p)
        assert.equal(r.status, 503, p)
        assert.deepEqual(r.json(), { detail: 'Cloudflare Access is not configured' })
        secured(r)
      }
      assert.equal((await as(null, '/pc/ops', { 'X-Assetlog-Key': KEY })).status, 503)
    })
  } finally {
    w.stop()
  }
  w = await worker([`PC_KEY:${KEY}`, `ACCESS_TEAM:${TEAM}`, `ACCESS_AUD:${AUD}`, `JWKS_URL:${JWKS_URL}`])
  try {
    await ok('DEV unset, Access configured: no token or a bad aud is 403, and JWKS_URL is never used', async () => {
      assert.equal((await as(null)).status, 403)
      assert.equal((await as(null, '/pc/ops', { 'X-Assetlog-Key': KEY })).status, 403)
      assert.equal((await as(jwt({ ...CLAIMS(), aud: ['other'] }))).status, 403)
      assert.equal(certsFetched, 1)
    })
  } finally {
    w.stop()
  }
  console.log(`\n${passed} checks passed`)
} finally {
  jwks.close()
  rmSync(PERSIST, { recursive: true, force: true })
}
