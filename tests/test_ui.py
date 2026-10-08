"""static/: sw.js runs under node with a fake Cache API and network; index.html runs in headless Chrome
(--dump-dom) against a fake API injected in place of Chart.js. Each browser test ends by writing JSON into
<pre id="t-out">. Headless Chrome keeps the window at least 500px wide and never finishes CacheStorage calls under
virtual time, so the overflow check uses a token longer than 500px and the page's `caches` is stubbed."""
import html
import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

STATIC = Path(__file__).resolve().parents[1] / "assetlog" / "static"
INDEX = (STATIC / "index.html").read_text()
NODE = shutil.which("node")
CHROME = next(filter(None, map(shutil.which, ("google-chrome", "chromium", "chromium-browser"))), None)
needs_node = pytest.mark.skipif(not NODE, reason="node not installed")
needs_chrome = pytest.mark.skipif(not CHROME, reason="chrome not installed")


# ---- static checks ----

def _luminance(hex_):
    hex_ = hex_.lstrip("#")
    hex_ = "".join(ch * 2 for ch in hex_) if len(hex_) == 3 else hex_
    c = [int(hex_[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_muted_text_meets_aa_on_card_and_page_in_both_themes():
    for block in re.findall(r":root\s*\{([^}]*)\}", INDEX):  # light, then dark
        tok = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{3,6})\b", block))
        for bg in ("card", "bg"):
            assert _contrast(tok["muted"], tok[bg]) >= 4.5, (tok["muted"], tok[bg])


def test_head_tags_for_access_and_cdn():
    assert re.search(r'<link rel="manifest"[^>]*crossorigin="use-credentials"', INDEX)  # else no Access cookie is sent
    assert re.search(r'<script src="https://cdn\.jsdelivr\.net/[^"]+" crossorigin="anonymous">', INDEX)  # cors: real status
    assert re.search(r'<link rel="stylesheet" href="https://cdn\.jsdelivr\.net/[^"]+pretendard[^"]*\.css">', INDEX)
    for cid in ("c-series", "c-years", "c-divs"):
        assert re.search(rf'<canvas id="{cid}" role="img" aria-label="[^"]+"', INDEX)


# ---- sw.js under node ----

SW_HARNESS = r"""
const fs = require('fs'), vm = require('vm')
const src = fs.readFileSync(process.argv[1], 'utf8'), S = 'https://app.test/api/summary', KEY = '/api/summary', CDN = 'https://cdn.jsdelivr.net/npm/chart.js@4'
function sw() {  // a fresh worker; `net` answers every fetch
  const names = new Map(), on = {}, t = { calls: [] }
  const open = n => { if (!names.has(n)) names.set(n, new Map()); const m = names.get(n)  // a handle outlives delete(), as in browsers
    return { match: async k => m.get(k.url || k)?.clone?.() ?? m.get(k.url || k), put: async (k, r) => { m.set(k.url || k, r) } } }
  const env = { self: { addEventListener: (k, f) => on[k] = f, skipWaiting() {}, clients: { claim() {} } }, location: { origin: 'https://app.test' },
    caches: { open: async n => open(n), keys: async () => [...names.keys()], delete: async n => names.delete(n) },
    fetch: (...a) => { t.calls.push(a); return t.net(...a) }, URL, Headers, Response, Promise, setTimeout, Date }
  vm.createContext(env); vm.runInContext(src, env)
  t.saved = key => [...names.values()].some(m => m.has(key))  // networkFirst saves under the path, cacheFirst the URL
  t.put = async (url, r) => (await open('assetlog-v1')).put(url, r)
  t.go = async (url = S, mode = 'cors') => {
    let p; on.fetch({ request: { url, method: 'GET', mode }, respondWith: x => p = x })
    const r = await p
    return { status: r.status, offline: r.headers.get('X-Offline'), body: r.text ? await r.clone().text() : '' }
  }
  t.message = async data => { let w; on.message?.({ data, waitUntil: p => w = p }); await w }  // ?.: an old worker has no listener
  return t
}
const reply = (status, body = '', ms = 0) => async () => { await new Promise(r => setTimeout(r, ms)); return new Response(status === 204 ? null : body, { status }) }
const gate = { status: 0, ok: false, type: 'opaqueredirect', redirected: false, headers: new Headers() }  // fetch(redirect: 'manual')
const out = {};
(async () => {
  let w = sw(); w.net = reply(200, 'old'); await w.go()
  w.net = reply(401, '{"detail":"login"}'); out.expired = await w.go(); out.expired_kept = w.saved(KEY)
  w.net = async () => { throw new TypeError('offline') }; out.expired_then_offline = await w.go().catch(e => String(e))

  w = sw(); w.net = reply(200, 'old'); await w.go()
  w.net = async () => gate; out.gate = await w.go(); out.gate_kept = w.saved(KEY)

  w = sw(); w.net = async () => { const r = new Response('login page'); Object.defineProperty(r, 'redirected', { value: true }); return r }
  await w.go(); out.redirected_kept = w.saved(KEY)

  w = sw(); w.net = reply(200, 'old'); await w.go()
  w.net = reply(530, '<html>Error 1033'); out.tunnel = await w.go()
  w = sw(); w.net = reply(502, 'bad gateway'); out.tunnel_nothing_saved = await w.go()

  w = sw(); w.net = reply(200, 'old'); await w.go()
  w.net = async () => { throw new TypeError('blip') }; out.blip = await w.go()
  w.net = reply(200, 'new', 5); out.back = await w.go()

  w = sw(); w.net = reply(200, 'old'); await w.go(); await w.message({ type: 'logout' }); out.logout_kept = w.saved(KEY)

  w = sw(); w.net = reply(404, '{"detail":"Not Found"}'); out.meta_pc = await w.go('https://app.test/api/meta'); out.meta_pc_kept = w.saved('/api/meta')
  w.net = reply(200, '{"mode":"cloud"}'); await w.go('https://app.test/api/meta')
  w.net = async () => { throw new TypeError('offline') }; out.meta_offline = await w.go('https://app.test/api/meta')

  w = sw(); w.net = reply(503, 'busy'); out.cdn_error = await w.go(CDN, 'no-cors'); out.cdn_error_kept = w.saved(CDN)
  out.cdn_mode = w.calls[0][1]?.mode
  w.net = reply(200, '/*chart*/'); await w.go(CDN, 'no-cors'); w.net = reply(503, 'busy'); out.cdn_later = await w.go(CDN)

  w = sw(); await w.put(CDN, { ok: false, status: 0, type: 'opaque', headers: new Headers() })  // saved by the old no-cors worker
  w.net = reply(200, '/*chart*/'); out.cdn_after_opaque = await w.go(CDN)
  console.log(JSON.stringify(out))
  process.exit(0)  // the worker's own wait timers would hold node for 5 s
})()
"""


@pytest.fixture(scope="module")
def sw():
    run = subprocess.run([NODE, "-e", SW_HARNESS, str(STATIC / "sw.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@needs_node
def test_sw_sign_in_answer_is_shown_and_wipes_saved_copies(sw):
    assert sw["expired"]["status"] == 401 and not sw["expired"]["offline"]
    assert not sw["expired_kept"]
    assert sw["expired_then_offline"] == "TypeError: offline"  # nothing left to show without a session
    assert sw["gate"]["status"] == 0 and not sw["gate_kept"]  # Access-style redirect, fetched with redirect 'manual'
    assert not sw["redirected_kept"]
    assert not sw["logout_kept"]


@needs_node
def test_sw_tunnel_error_falls_back_but_a_server_that_is_back_wins(sw):
    assert sw["tunnel"] == {"status": 200, "offline": "1", "body": "old"}
    assert sw["tunnel_nothing_saved"]["status"] == 502
    assert sw["blip"]["offline"] == "1"
    assert sw["back"] == {"status": 200, "offline": None, "body": "new"}  # was the stale copy for 30 s


@needs_node
def test_sw_reads_cloud_meta_network_first(sw):
    assert sw["meta_pc"]["status"] == 404 and not sw["meta_pc_kept"]  # the PC app: local mode, nothing saved
    assert sw["meta_offline"] == {"status": 200, "offline": "1", "body": '{"mode":"cloud"}'}  # cloud mode survives offline


@needs_node
def test_sw_keeps_only_a_good_cdn_copy(sw):
    assert sw["cdn_error"]["status"] == 503 and not sw["cdn_error_kept"]
    assert sw["cdn_mode"] == "cors"
    assert sw["cdn_later"]["status"] == 200 and sw["cdn_later"]["body"] == "/*chart*/"
    assert sw["cdn_after_opaque"]["status"] == 200  # an opaque copy can't answer the cors request


# ---- pages in headless Chrome ----

class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.pages.get(urlsplit(self.path).path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def log_message(self, *a):
        pass


# where a page lands after navigating: prints what the previous page saved in sessionStorage
LANDED = """<pre id="t-out"></pre><script>
document.getElementById('t-out').textContent = JSON.stringify({ ...JSON.parse(sessionStorage.t || '{}'), landed: location.pathname })
</script>"""


def browse(tmp_path, pages, path="/"):
    """Serve `pages` ({path: html}), open `path` in headless Chrome and return the JSON the page left in #t-out."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    srv.pages = pages
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        run = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                              f"--user-data-dir={tmp_path / 'chrome'}", "--virtual-time-budget=20000", "--dump-dom",
                              f"http://127.0.0.1:{srv.server_port}{path}"], capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
    m = re.search(r'<pre id="t-out">(.*?)</pre>', run.stdout, re.S)
    assert m, run.stdout[-3000:] + run.stderr[-2000:]
    return json.loads(html.unescape(m.group(1)))


# Replaces Chart.js (no network in tests): a fake API, then scenario steps that call the page's own functions.
INDEX_STUB = r"""<script>(() => {
const T = { calls: [] }, save = () => sessionStorage.t = JSON.stringify(T), mode = new URLSearchParams(location.search).get('mode');
const prev = JSON.parse(sessionStorage.t || 'null'), cloud = mode?.startsWith('cloud'), Q = { queued: 5, detail: 'PC가 켜지면 반영돼요' };
const ERR_OP = { id: 9, at: '2026-09-25T14:30:00Z', method: 'POST', path: '/api/snapshots', status: 'error', result: '모델 값과 7% 달라요 (54,000,000원)' };
const META = { mode: 'cloud', pushed_at: '2026-09-26T00:05:00+09:00', pending: 2, recent: [
  ERR_OP, { id: 8, at: '2026-09-25T14:00:00Z', method: 'PATCH', path: '/api/transactions/7', status: 'done', result: null }],
  errors: [ERR_OP] };  // the Worker keeps errors separate from recent (see cloud/src/index.js's meta())
Object.defineProperty(window, 'caches', { value: { keys: async () => ['assetlog-v1'], delete: async k => { T.wiped = k; save(); return true; } } });
const S = { as_of: '2026-09-26', total_value: 3000000, fx: { USD: { krw: 1400, date: '2026-09-26' } }, unknown_count: 1,
  ytd: null, ttm: null, years: [], since_start: null, dividends_by_month: { '2026-03': 150000 },
  series: [{ date: '2026-09-26', value: 3000000, principal: 3000000 }],
  accounts: [{ id: 'a', name: 'A', institution: 'kis', kind: 'brokerage', value: 1000000, as_of: '2026-09-26', ytd: null },
             { id: 'b', name: 'B', institution: 'toss', kind: 'brokerage', value: 2000000, as_of: '2026-09-26', ytd: null }] };
const LONG = 'error: HTTPError 500 for url: https://openapi.koreainvestment.com:9443/uapi/overseas/inquire?CANO=XXXXXXXX&ACNT_PRDT_CD=01&CTX_AREA_FK200=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const R = {
  'GET /api/summary': [200, S],
  'GET /api/accounts': [200, S.accounts],
  'GET /api/snapshots': u => [200, [{ date: '2026-09-25', value: 1000000, source: 'manual' }], u.searchParams.get('account_id') === 'b' ? 300 : 0],
  'DELETE /api/snapshots': cloud ? [202, Q] : [200, { deleted: 1 }],
  'GET /api/meta': cloud ? [200, META] : [404, { detail: 'Not Found' }],  // the PC app has no /api/meta
  'POST /api/snapshots': cloud ? [202, Q] : (u, o) => JSON.parse(o.body).force ? [200, { ok: true }]
    : [409, { detail: { code: 'dc_mismatch', message: '모델 값과 7% 달라요. 그래도 저장할까요?', model: 1000 } }],
  'PATCH /api/transactions/7': cloud ? [202, Q] : [200, { id: 7, kind: 'UNKNOWN' }],
  'GET /api/transactions': [200, [{ id: 7, account_id: 'a', date: '2026-09-01', kind: 'UNKNOWN', amount: 2000000, memo: '타사대체입고',
    source: 'kis_xlsx', kind_locked: 0, raw: '{"shown": "2,000,000원"}' }]],
  'GET /api/holdings': [200, { fetched_at: '2026-09-26T10:00:00+09:00', accounts: [{ account_id: 'a', name: 'A', institution: 'kis',
    source: 'snapshot', error: LONG, total_krw: 1000000, as_of: '2026-09-25', positions: [] }] }],
  'GET /api/import/formats': [200, { csv: 'CSV' }],
  'POST /api/sync': [202, Q], 'POST /api/import': [202, Q],  // cloud/ queues every write
};
window.fetch = async (path, opt = {}) => {
  const u = new URL(path, location.href), key = (opt.method || 'GET') + ' ' + u.pathname, route = R[key];
  T.calls.push([key + u.search, opt.body ?? null]); save();
  const [status, body, ms = 0] = typeof route === 'function' ? route(u, opt) : route || [404, { detail: key }];
  await new Promise(r => setTimeout(r, ms));
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
};
window.confirm = m => { T.asked = m; return true; };
const $ = s => document.querySelector(s), wait = ms => new Promise(r => setTimeout(r, ms));
const step = async (name, fn) => { try { T[name] = await fn(); } catch (e) { T[name] = 'ERROR ' + e; } save(); };
const finish = (t = T) => { const pre = document.createElement('pre'); pre.id = 't-out'; pre.textContent = JSON.stringify(t); document.body.append(pre); };
addEventListener('load', async () => {
  if (mode === 'cloud-expired' && prev?.expired) return finish({ ...prev, reloaded: location.search });  // the page came back
  await wait(200);  // the page's own load()
  if (mode === 'cloud-logout') return $('#logout-btn').click();
  if (mode === 'cloud-expired') { T.expired = true; save(); R['GET /api/transactions'] = [401, {}]; return loadReview(); }
  if (mode === 'cloud') return cloudSteps().then(() => finish());
  if (mode) return;
  await step('local', () => [$('#cloud').hidden, $('#sync-btn').textContent, $('#f-inbox').hidden, $('#hd-refresh').disabled,
    $('#logout-card').hidden]);
  await step('cards', () => [$('#growth').textContent, $('#divs').textContent]);
  await step('sync_banner', () => {  // the last sync rides on the summary: no request of its own
    const ago = h => new Date(Date.now() - h * 36e5).toISOString(), n = T.calls.length;
    const home = sync => { renderHome({ ...S, unknown_count: 0, sync }); return $('#alerts').textContent.replace(/\s+/g, ' ').trim(); };
    const out = [home({ at: ago(1), ok_at: ago(1), failed: [] }), home(undefined),
      home({ at: ago(1), ok_at: ago(49), failed: [{ source: 'toss', message: 'HTTP 403 <b>x</b>', hint: 'IP를 등록하세요' },
        { source: 'kb', message: 'KB token: <i>bad</i>', hint: null }] }),
      home({ at: ago(3 * 24), ok_at: ago(3 * 24), failed: [] }), home({ at: ago(30), ok_at: null, failed: [] }),
      T.calls.length - n];
    renderHome(S); return out;
  });
  await step('since', () => sinceCard({ ...S, since_start: { cumulative: 0.2, annual: 0.13 }, years: [
    { year: 2025, return: 0.1, eff_start: '2025-06-30', span_start: '2025-03-31' }, { year: 2026, return: 0.09, eff_start: '2025-12-31', span_start: '2025-12-31' }] }));
  await step('returns_usd', () => {  // 달러 기준 + 환율 영향: shown when return_usd exists, hidden when it's null
    state.retPeriod = 'ytd';
    const withUsd = { return: 0.15, return_ex_div: 0.12, return_usd: 0.1, fx_effect: 0.05, return_annualized: null };
    const shown = returnsCard({ ...S, ytd: withUsd, ttm: null, years: [] });
    const noUsd = returnsCard({ ...S, ytd: { ...withUsd, return_usd: null, fx_effect: null }, ttm: null, years: [] });
    toggleView('hide');  // percentages are never masked, only won() amounts are
    const hidden = returnsCard({ ...S, ytd: withUsd, ttm: null, years: [] });
    toggleView('hide');
    const acct = accountRow({ name: 'A', institution: 'kis', kind: 'brokerage', value: 1000000, as_of: S.as_of, ytd: withUsd }, S.as_of);
    return { has: [shown.includes('달러 기준'), shown.includes('▲+10.0%'), shown.includes('환율 영향'), shown.includes('▲+5.0%p')],
      absent: [noUsd.includes('달러 기준'), noUsd.includes('환율 영향')], unmasked: [hidden.includes('▲+10.0%'), hidden.includes('▲+5.0%p')],
      acct: acct.includes('달러') };  // the per-account line: fitsAcctSub used to hide this at every real width
  });
  await step('holdings_fx', () => {  // 보유 tab: local-currency P&L under the KRW one, and the FX-inclusion wording
    state.holdings = { fetched_at: '2026-09-26T10:00:00+09:00', accounts: [
      { account_id: 'kis', name: 'KIS 해외', institution: 'kis', source: 'live', error: null, total_krw: 200000,
        positions: [{ symbol: 'AAPL', name: '애플', currency: 'USD', quantity: 1, price: 150, value_krw: 200000, cost_krw: 180000,
          pnl_krw: 20000, pnl_rate: 0.111, pnl_ex_fx: false, pnl_local_rate: 0.062, annual_div_krw: 0, div_yield: 0 }] },
      { account_id: 'toss', name: '토스 해외', institution: 'toss', source: 'live', error: null, total_krw: 100000,
        positions: [{ symbol: 'SCHD', name: 'SCHD', currency: 'USD', quantity: 1, price: 80, value_krw: 100000, cost_krw: 95000,
          pnl_krw: 5000, pnl_rate: 0.0526, pnl_ex_fx: true, pnl_local_rate: 0.0526, annual_div_krw: 0, div_yield: 0 }] }] };
    // toss.py sets pnl_rate === pnl_local_rate (no separate KRW cost per item), so the KRW figure already IS the
    // local one; only KIS (pnl_ex_fx false) should repeat it on a second "$ 기준" line
    renderHoldings();
    const html = $('#hd-body').innerHTML;
    return { local_pnl: html.match(/\$ 기준 ▲\+?6\.2%/g)?.length ?? 0, fx_included: html.includes('환율 변동 포함'), fx_excluded: html.includes('환율 변동 제외') };
  });
  await step('review_usd', async () => { view.usd = true; await loadReview(); const i = $('#rv-list input'); view.usd = false; return [i.disabled, i.getAttribute('aria-label')]; });
  await step('review_won', async () => { await loadReview(); const i = $('#rv-list input'); return [i.disabled, i.getAttribute('aria-label'), getComputedStyle(i).fontSize, getComputedStyle($('#rv-list select')).fontSize]; });
  await step('hidden', async () => {
    state.sync = { dc: 'ok: 412 shares + 3,210원 cash = 54,321,000원 on 2026-09-26' }; renderSync();
    toggleView('hide'); await loadReview();
    const seen = [$('#sync-out').textContent, !!$('#rv-list details')];
    toggleView('hide'); return seen;
  });
  await step('review_busy', async () => {  // a card waiting for its answer stays locked through a re-render
    R['PATCH /api/transactions/7'] = [200, { id: 7, kind: 'UNKNOWN' }, 300];
    $('#rv-list button[data-act="save"]').click(); await wait(50);
    renderReview(); const locked = $('#rv-list fieldset').disabled;
    await wait(600); R['PATCH /api/transactions/7'] = [200, { id: 7, kind: 'UNKNOWN' }];
    return [locked, $('#rv-list fieldset').disabled];
  });
  await step('local_save', async () => {  // an applied write re-reads, and a revisit fetches again
    const n = T.calls.length;
    $('#rv-list button[data-act="save"]').click(); await wait(100);
    show('home'); show('review'); await wait(50);
    return T.calls.slice(n).map(c => c[0]);
  });
  await step('unknown_none', async () => {  // the PC's list is cheap and its summary may be stale: still fetched
    const n = T.calls.length; state.summary.unknown_count = 0;
    await loadReview(); state.summary.unknown_count = 1;
    return [T.calls.length - n, $('#rv-list').textContent.trim()];
  });
  await step('snap_delete', async () => {
    show('input'); await wait(50);
    const f = $('#f-snap'); f.account_id.value = 'a'; await loadSnaps();
    const btn = $('#snap-list button[data-date]');
    f.account_id.value = 'b'; btn.click(); await wait(50);  // a's row is still on screen
    return T.calls.filter(c => c[0].startsWith('DELETE')).map(c => c[0]);
  });
  await step('snap_switch', async () => {
    const f = $('#f-snap');
    f.account_id.value = 'b'; f.account_id.dispatchEvent(new Event('change'));
    const cleared = $('#snap-list').children.length;
    f.account_id.value = 'a'; f.account_id.dispatchEvent(new Event('change'));  // b's slow answer comes last
    await wait(500); return [cleared, $('#snap-list button')?.dataset.acc];
  });
  await step('dc_mismatch', async () => {
    const f = $('#f-snap'); f.account_id.value = 'a'; f.date.value = '2026-09-25'; f.value.value = '1000';
    f.requestSubmit(); await wait(200);
    return [T.asked, T.calls.filter(c => c[0] === 'POST /api/snapshots').map(c => JSON.parse(c[1]).force ?? null)];
  });
  await step('overflow', async () => {
    const over = () => document.documentElement.scrollWidth - document.documentElement.clientWidth;
    show('holdings'); await wait(100);
    const holdings = over();
    show('input'); state.sync = { kis: LONG }; renderSync();
    return [holdings, over()];
  });
  finish();
});
async function cloudSteps() {
  const metas = () => T.calls.filter(c => c[0] === 'GET /api/meta').length, said = () => { const t = $('#toast').textContent; $('#toast').textContent = ''; return t; };
  await step('cloud_ui', () => ({ bar: $('#cloud').textContent, errs: $('#cloud').querySelectorAll('p').length, sync: $('#sync-btn').textContent,
    inbox_hidden: $('#f-inbox').hidden, refresh_disabled: $('#hd-refresh').disabled, hint: !$('#hd-hint').hidden, logout: !$('#logout-card').hidden }));
  await step('cloud_hidden', () => { toggleView('hide'); const t = $('#cloud').textContent; toggleView('hide'); return t; });
  await step('cloud_holdings', async () => { show('holdings'); await wait(100); return [$('#hd-refresh').disabled, !!$('#hd-body .card')]; });
  await step('cloud_review', async () => {
    const reads = () => T.calls.filter(c => c[0].startsWith('GET /api/') && c[0] !== 'GET /api/meta').length;
    show('review'); await wait(50); const before = reads();
    $('#rv-list button[data-act="save"]').click(); await wait(100);
    const marked = [$('#rv-list fieldset').disabled, $('#rv-list .badge')?.textContent, said()];
    toggleView('usd'); toggleView('hide'); toggleView('usd'); toggleView('hide');
    show('holdings'); show('review'); await wait(50);
    return { marked, still: $('#rv-list fieldset').disabled, reads: reads() - before };
  });
  await step('cloud_unknown_none', async () => {  // the summary says none are left: no ledger scan on the Worker
    const n = T.calls.length; state.summary.unknown_count = 0;
    await loadReview(); state.summary.unknown_count = 1;
    return [T.calls.length - n, $('#rv-list').textContent.trim()];
  });
  await step('cloud_snap', async () => {
    show('input'); await wait(50); const before = metas();
    const f = $('#f-snap'); f.account_id.value = 'a'; f.date.value = '2026-09-25'; f.value.value = '1000';
    f.requestSubmit(); await wait(200);
    return { toast: said(), asked: T.asked ?? null, meta_refreshed: metas() > before, cleared: f.value.value === '' };
  });
  await step('cloud_snap_delete', async () => {  // queued: marked, and no button left to send it twice
    const f = $('#f-snap'); f.account_id.value = 'a'; await loadSnaps();
    $('#snap-list button[data-date]').click(); await wait(100); said();
    return [$('#snap-list .badge')?.textContent, !!$('#snap-list button[data-date]')];
  });
  await step('cloud_sync', async () => {
    $('#sync-btn').click(); await wait(200);
    return [said(), $('#sync-out').children.length, $('#sync-btn').textContent];
  });
  await step('cloud_import', async () => {
    const f = $('#f-import'), dt = new DataTransfer(); dt.items.add(new File(['date,amount'], 'x.csv')); f.file.files = dt.files;
    f.account_id.value = 'a'; f.requestSubmit(); await wait(200);
    return [said(), $('#im-out').hidden];
  });
  await step('cloud_once', async () => {
    await loadMeta(); const again = $('#cloud').querySelectorAll('p').length;
    state.errs = []; await loadMeta();  // as after a reload: the device remembers what it showed
    return [again, $('#cloud').querySelectorAll('p').length, localStorage.getItem('op-err')];
  });
}
})();</script>"""


def _index_page():
    page, n = re.subn(r'<script src="https://cdn\.jsdelivr\.net[^>]*></script>', lambda _: INDEX_STUB, INDEX)
    assert n == 1
    # no network in tests (see the module docstring): the Pretendard CSS (and its ~92 font url()s) would otherwise
    # be fetched from cdn.jsdelivr.net by every headless-Chrome test below
    page = re.sub(r'<link rel="stylesheet" href="https://cdn\.jsdelivr\.net[^>]*>', '', page)
    return page


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    return browse(tmp_path_factory.mktemp("ui"), {"/": _index_page()})


@needs_chrome
def test_empty_period_cards_name_the_real_reason(ui):
    growth, divs = ui["cards"]
    assert "두 번 이상" in growth and "두 번 이상" in divs  # was "배당 기록이 아직 없어요" beside a ₩150,000 bar
    assert "2025년 3월 31일부터" in ui["since"] and "1.5년" in ui["since"]  # span_start, as compound() annualizes


@needs_chrome
def test_returns_card_shows_dollar_basis_and_never_masks_it(ui):
    r = ui["returns_usd"]
    assert r["has"] == [True, True, True, True]  # 달러 기준, its figure, 환율 영향, its %p figure
    assert r["absent"] == [False, False]  # return_usd null: neither shown, nothing said
    assert r["unmasked"] == [True, True]  # the hide toggle masks won() amounts, not percentages
    assert r["acct"]  # accountRow shows the per-account 달러 기준 line too


@needs_chrome
def test_holdings_show_local_currency_pnl_and_consistent_fx_wording(ui):
    r = ui["holdings_fx"]
    assert r["local_pnl"] == 1  # KIS only: Toss's KRW pnl already IS the local-currency figure (pnl_ex_fx)
    assert r["fx_included"] and r["fx_excluded"]  # KIS: 환율 변동 포함, Toss: 환율 변동 제외


@needs_chrome
def test_sync_failure_banner_and_staleness_note(ui):
    fine, none, failed, stale, recent, requests = ui["sync_banner"]
    assert fine == none == ""  # all fine, or a PC that never recorded a sync: nothing shown
    assert failed.startswith("!동기화 실패 · ") and "토스증권: IP를 등록하세요" in failed  # the hint, not the raw message
    assert "KB증권: KB token: <i>bad</i>" in failed  # no hint: the message, escaped (as markup it would read "bad")
    assert "마지막 성공 " in failed and "403" not in failed and requests == 0  # nothing fetched for it
    assert stale == "마지막 동기화: 3일 전 (PC가 꺼져 있었다면 정상이에요)" and recent == ""  # only past 36 hours


@needs_chrome
def test_review_amount_is_won_and_16px(ui):
    assert ui["review_usd"] == [True, "금액 수정 (원)"]  # $ mode: no field that looks like dollars but saves won
    assert ui["review_won"] == [False, "금액 수정 (원)", "16px", "16px"]  # iOS zooms into anything smaller


@needs_chrome
def test_hide_masks_sync_report_and_raw_rows(ui):
    sync, raw_shown = ui["hidden"]
    assert "완료" in sync and not re.search(r"\d", sync)
    assert raw_shown is False


@needs_chrome
def test_snapshot_delete_targets_the_listed_account(ui):
    assert ui["snap_delete"] == ["DELETE /api/snapshots?account_id=a&date=2026-09-25"]
    assert ui["snap_switch"] == [0, "a"]  # cleared at once; a slow earlier answer doesn't overwrite


@needs_chrome
def test_dc_mismatch_asks_then_forces(ui):
    assert ui["dc_mismatch"] == ["모델 값과 7% 달라요. 그래도 저장할까요?", [None, True]]


@needs_chrome
def test_long_error_text_wraps_inside_the_page(ui):
    assert ui["overflow"] == [0, 0]


@needs_chrome
def test_pc_app_without_meta_stays_in_local_mode(ui):
    assert ui["local"] == [True, "동기화", False, False, True]  # no banner, same sync, capture and refresh; no logout


@needs_chrome
def test_local_writes_re_read_and_tabs_fetch_on_every_visit(ui):
    tx = "GET /api/transactions?limit=200&kind=UNKNOWN"
    assert ui["local_save"] == ["PATCH /api/transactions/7", "GET /api/summary", "GET /api/accounts", tx, tx]
    assert ui["unknown_none"][0] == 1  # unknown_count 0 may be stale here (the 23:50 sync): fetched anyway
    assert ui["review_busy"] == [True, False]  # locked while waiting, also after a re-render; unlocked once answered


@pytest.fixture(scope="module")
def cloud(tmp_path_factory):
    return browse(tmp_path_factory.mktemp("cloud"), {"/": _index_page()}, "/?mode=cloud")


@needs_chrome
def test_cloud_banner_shows_push_time_pending_and_each_error_once(cloud):
    ui = cloud["cloud_ui"]
    assert ui["bar"].startswith("PC 동기화 기준 9월 26일 00:05 · 반영 대기 2건")  # KST, 24-hour
    assert "반영하지 못했어요 · 9월 25일 23:30 평가액 입력: 모델 값과 7% 달라요 (54,000,000원)" in ui["bar"]  # at is UTC
    assert ui["errs"] == 1  # only the op with status error
    assert "54,000,000" not in cloud["cloud_hidden"] and "••" in cloud["cloud_hidden"]  # the hide toggle covers it
    assert cloud["cloud_once"] == [1, 0, "9"]  # not repeated on a refresh, nor after a reload


@needs_chrome
def test_cloud_mode_swaps_what_needs_the_pc(cloud):
    ui = cloud["cloud_ui"]
    assert ui["sync"] == "동기화 요청" and not ui["inbox_hidden"] and ui["refresh_disabled"] and ui["hint"] and ui["logout"]
    assert cloud["cloud_holdings"] == [True, True]  # still disabled after the holdings load


@needs_chrome
def test_cloud_writes_are_queued_not_expected_to_apply(cloud):
    assert cloud["cloud_snap"] == {"toast": "PC가 켜지면 반영돼요", "asked": None, "meta_refreshed": True, "cleared": True}
    assert cloud["cloud_sync"] == ["PC가 켜지면 반영돼요", 0, "동기화 요청"]  # no broker report to render
    assert cloud["cloud_import"] == ["PC가 켜지면 반영돼요", True]  # no import result yet


@needs_chrome
def test_cloud_queued_review_is_marked_and_nothing_is_re_read(cloud):
    r = cloud["cloud_review"]
    assert r["marked"] == [True, "반영 대기", "PC가 켜지면 반영돼요"] and r["still"]  # locked, also after re-renders
    assert r["reads"] == 0  # no re-read after the queued write, the ₩/$ and hide toggles, or a tab revisit
    assert cloud["cloud_unknown_none"] == [0, "분류할 거래가 없어요. 모두 정리됐어요."]  # unknown_count 0: no ledger scan
    assert cloud["cloud_snap_delete"] == ["반영 대기", False]  # a queued delete can't be sent twice


@needs_chrome
def test_cloud_logout_wipes_saved_copies_then_ends_the_access_session(tmp_path):
    t = browse(tmp_path, {"/": _index_page(), "/login": LANDED, "/cdn-cgi/access/logout": LANDED}, "/?mode=cloud-logout")
    assert t["wiped"] == "assetlog-v1" and t["landed"] == "/cdn-cgi/access/logout"
    assert not any(c[0] == "POST /api/logout" for c in t["calls"])


@needs_chrome
def test_cloud_expired_access_session_wipes_and_reloads(tmp_path):
    t = browse(tmp_path, {"/": _index_page(), "/login": LANDED}, "/?mode=cloud-expired")
    assert t["wiped"] == "assetlog-v1" and t["reloaded"] == "?mode=cloud-expired" and "landed" not in t  # not /login
