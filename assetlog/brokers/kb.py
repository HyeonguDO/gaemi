"""KB증권 Open API (B2C, production only): value snapshot + SWQA2301 cash ledger for one account.

No request names an account: the key pair is bound to the account picked on the KB homepage when applying
for the API, so each KB account needs its own key pair (and this module a second config prefix).

Snapshot value = domestic + overseas, each from the TR that owns it, so nothing is counted twice:
- domestic (SSQM2952): Σ val_amt of the KRW rows (정산수량 basis, so unsettled buys are in) + nxt2_dy_tfnd
  (D+2 예수금, net of those same trades). In the portal sample that is exactly the header nt_asts_val_amt
  (426500 + 639950 = 1066450). The header may also hold the foreign rows + FX cash (then SPQM2226 supplies
  them instead); any other gap (loans, 금융상품 without a row) raises.
- overseas (SPQM2226): Σ frgn_hld_q_p6 × now_prc_p4 × std_exch_r over the holdings, checked against the
  header totals, + Σ tfnd × std_exch_r over the foreign-currency cash rows.
cash = nxt2_dy_tfnd (KRW only).

Transactions: SWQA2301 in WINDOW-day slices from `since` (or HISTORY_START), every row emitted, classify()
reads smry_nm (적요명). Amounts come unsigned, so the signed KRW cash effect of a row is how far the running
예수금 balance tfnd_blnc moved since the previous row (sorted by dl_dt, dl_sq). For every row that counts
(not INTERNAL) that move must equal ec_amt (정산금액), else the sync fails; so does a blank tfnd_blnc. The one
exception is a foreign-currency row that leaves KRW cash untouched (below). Dividends are therefore the net
cash received; a separate tax row nets into DIVIDEND only if its memo contains 배당.

Unverified against a live account (python3 -m assetlog.brokers.kb prints probe() to check):
- SWQA2301 body: the portal sample's 19 keys (inq_clsf '1' = 전체, inq_clsf1..6 '0'), not the 5 the page shows.
  If dividend or interest rows are missing, try inq_clsf1..6 = '1' (what kr-broker sends).
- SWQA2301 max range and page size are unpublished: WINDOW days per call, nxt_key echoed back verbatim,
  at most MAX_PAGES pages per call.
  An empty slice is taken from processFlag A, a NO_DATA code or a '자료가 없'/'내역이 없' message; anything
  else raises. The first sync walks back to HISTORY_START: if KB refuses old dates, move it.
- Sign rule: only 입금 rows have been seen. The balance before a full history is taken as 0; if that
  contradicts the first row's ec_amt (cash held before HISTORY_START) the row is kept with amount 0 as UNKNOWN
  (INTERNAL stays INTERNAL) for the review queue. In an incremental sync the first row fetched only seeds the
  balance: it lies in sync's refetch window, stored earlier (a row back-dated before all others there is missed).
- Foreign-currency rows (fcrncy_amt != 0) appear in no sample. One that leaves KRW cash untouched (a USD
  dividend or deposit) has no KRW amount here: unless classify() calls it INTERNAL it goes to the review
  queue as UNKNOWN with its raw row. One that moves tfnd_blnc is checked against ec_amt like any other.
- SSQM2952: nt_asts_val_amt composition (above); a row is domestic when crncy_cd is blank or KRW.
- SPQM2226 (std_crncy_f '1', exch_r_aplc_f '2', fee_clsf '1' as kr-broker sends; mktpr_aplc_clsf '' since
  its values are undocumented; cn_f 'Y' on follow-up pages, inferred): now_prc_p4 and tfnd are taken in the
  row currency and std_exch_r as KRW per unit (a JPY rate per 100 would fail the header check). Holdings
  must be within 1% of tl_scrts_exch_val_amt or tl_krw_val_amt. FX cash has no check (units of
  tl_tfnd_exch_val_amt unknown). Settlement basis: frgn_hld_q_p6 (보유수량) is read like SSQM2952 hld_q, which
  leaves out an unsettled buy, and tfnd like dy_tfnd, not yet debited for it, so the pair agrees. If probe()
  on a day with an unsettled overseas buy shows the new shares in frgn_hld_q_p6 while tfnd still holds their
  cost, that day's snapshot counts the buy twice.
- dataHeader: this host's LAN IP and MAC (XX-XX-XX-XX-XX-XX); set KB_IP_ADDR / KB_MAC_ADDR if KB wants
  others (E031 = IP not allowed).
- Error codes are looked for in processCode and resultCode. The token is reissued once on T041-T044/I445;
  kr-broker saw a reissue return the same token, in which case the retry fails loudly. GAP between calls,
  one 3 s back-off on E041/E042/I435 (rateLimitRule 200/5/3 has no documented units).
"""
import socket
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import requests

from .. import config, db

BASE = "https://developer.kbsec.com:32484"
REQUIRED = ("KB_APP_KEY", "KB_APP_SECRET")
GAP = 0.4                                             # s before every TR call
BUSY = {"E041", "E042", "I435"}                       # rate limit: wait 3 s, retry once
BAD_TOKEN = {"T041", "T042", "T043", "T044", "I445"}  # reissue once
NO_DATA = {"1861", "2149", "G533", "I749"}
MAX_PAGES = 500                                       # per pages() call: a key that never runs out must not loop
HISTORY_START = date(2015, 1, 1)
WINDOW = 90                                           # days per SWQA2301 call
DOMESTIC = {"excg_mktpr_ccd": "A"}
OVERSEAS = {"std_crncy_f": "1", "exch_r_aplc_f": "2", "fee_clsf": "1", "srt_clsf": "1", "cn_f": "", "nxt_key": "",
            **dict.fromkeys(("rsrv_ordr_f", "tl_asts_exch_val_amt", "tl_tfnd_exch_val_amt", "tl_scrts_exch_val_amt",
                             "tl_krw_val_amt", "tl_krw_val_pl_amt", "mktpr_aplc_clsf"), "")}
OVERSEAS_TOTALS = ("tl_scrts_exch_val_amt", "tl_krw_val_amt")
PII = {"cprty_ac_nm"}                                 # counterparty account names: masked in probe()


def _num(row, key):
    """'  1,000,000' / '000000000001000000' / '-00000000000000561' / '000018.45' -> Decimal; blank -> 0."""
    try:
        return Decimal(str(row[key]).strip().replace(",", "") or "0")
    except (KeyError, ArithmeticError):
        raise RuntimeError(f"KB: need a number in {key}, got {row.get(key)!r} in {str(row)[:300]}") from None


def _device(cfg):
    ip = cfg.get("KB_IP_ADDR")
    if not ip:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it only picks the outbound interface
                ip = s.getsockname()[0]
            except OSError:
                ip = "127.0.0.1"
    mac = cfg.get("KB_MAC_ADDR") or "-".join(f"{uuid.getnode():012X}"[i:i + 2] for i in range(0, 12, 2))
    return {"ipAddr": ip, "macAddr": mac}


def _why(env):
    h, b = env["dataHeader"], env.get("dataBody") or {}
    return f"[{h.get('resultCode')}/{h.get('processCode')}] {h.get('processMessage') or ''} {b.get('o_msg') or ''}".strip()


class _Api:
    def __init__(self, cfg):
        self.cfg, self.head = cfg, _device(cfg)
        self.token = self._token()

    def _send(self, path, body, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers |= {"Authorization": f"bearer {self.token}", "appKey": self.cfg["KB_APP_KEY"]}
        r = requests.post(BASE + path, json={"dataHeader": self.head, "dataBody": body}, headers=headers, timeout=30)
        try:  # business errors can come back as HTTP 500 with the envelope: parse it, not the status
            env = r.json()
            env["dataHeader"]["processFlag"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"KB {path}: HTTP {r.status_code}, no dataHeader: {r.text[:200]!r}") from None
        return env

    def _token(self, force=False):
        def issue():
            env = self._send("/oauth2/token", {"appKey": self.cfg["KB_APP_KEY"], "appSecret": self.cfg["KB_APP_SECRET"],
                                               "grantType": "client_credentials"}, auth=False)
            b = env.get("dataBody") or {}
            if env["dataHeader"]["processFlag"] != "A" or not b.get("access_token"):
                raise RuntimeError(f"KB token: {_why(env)}")
            return b["access_token"], float(b["expires_in"])
        return config.cached_token("kb", issue, margin=float("inf") if force else 600)  # inf: skip the cache

    def raw(self, tr, body):
        """Envelope of one call; reissues the token once on T041-T044/I445, backs off once on a rate limit."""
        retried = set()
        while True:
            time.sleep(GAP)
            env = self._send(f"/api/v1/{tr.lower()}", body)
            codes = {env["dataHeader"].get("processCode"), env["dataHeader"].get("resultCode")}
            if codes & BAD_TOKEN and "token" not in retried:
                retried.add("token")
                self.token = self._token(force=True)
            elif codes & BUSY and "busy" not in retried:
                retried.add("busy")
                time.sleep(3)
            else:
                return env

    def pages(self, tr, body):
        """dataBody of every page ({} for 'no data'), echoing nxt_key back (and cn_f 'Y' where the TR has it)."""
        out, seen = [], set()
        for _ in range(MAX_PAGES):
            env = self.raw(tr, body)
            b, why = env.get("dataBody") or {}, _why(env)
            if env["dataHeader"]["processFlag"] != "A":
                if env["dataHeader"].get("processCode") in NO_DATA or "자료가 없" in why or "내역이 없" in why:
                    return out + [{}]
                raise RuntimeError(f"KB {tr}: {why}")
            out.append(b)
            key = b.get("nxt_key") or ""
            if not key.strip():
                return out
            if key in seen:
                raise RuntimeError(f"KB {tr}: nxt_key {key.strip()!r} repeated")
            seen.add(key)
            body = {**body, "nxt_key": key, **({"cn_f": "Y"} if "cn_f" in body else {})}
        raise RuntimeError(f"KB {tr}: still a new nxt_key after {MAX_PAGES} pages")


def _rows(bodies):
    return [r for b in bodies for k, v in b.items() if k.startswith("Record") for r in v]


def _domestic(b):
    """SSQM2952 -> (KRW value, KRW cash)."""
    krw = fx = Decimal(0)
    for r in b.get("Record1") or []:
        if r.get("crncy_cd", "").strip() in ("", "KRW"):
            krw += _num(r, "val_amt")
        else:
            fx += _num(r, "val_amt")
    cash, total = _num(b, "nxt2_dy_tfnd"), _num(b, "nt_asts_val_amt")
    fx += _num(b, "fcrncy_tfnd_krw_exch_amt")
    if not any(abs(total - x) <= 1 for x in (krw + cash, krw + cash + fx)):
        raise RuntimeError(
            f"KB SSQM2952: nt_asts_val_amt={total} is neither KRW rows Σval_amt {krw} + nxt2_dy_tfnd {cash} nor that "
            f"+ foreign rows/fcrncy_tfnd_krw_exch_amt {fx}: loans or 금융상품? header {str({k: v for k, v in b.items() if k != 'Record1'})[:600]}")
    return krw + cash, cash


def _overseas(bodies):
    """SPQM2226 pages -> KRW value of foreign holdings + foreign-currency cash (the grids are told apart by field names)."""
    stocks = cash = Decimal(0)
    for r in _rows(bodies):
        if "frgn_hld_q_p6" in r:
            stocks += _num(r, "frgn_hld_q_p6") * _num(r, "now_prc_p4") * _num(r, "std_exch_r")
        elif "is_cd" not in r and r.get("crncy_clsf_nm", "").strip() not in ("KRW", "원화") and _num(r, "tfnd"):
            if not _num(r, "std_exch_r"):
                raise RuntimeError(f"KB SPQM2226: FX cash without std_exch_r: {str(r)[:300]}")
            cash += _num(r, "tfnd") * _num(r, "std_exch_r")
    hdr = bodies[0]
    totals = [_num(hdr, k) for k in OVERSEAS_TOTALS] if hdr else [Decimal(0)]
    if all(abs(stocks - t) > stocks / 100 + 1 for t in totals):
        raise RuntimeError(f"KB SPQM2226: holdings Σ frgn_hld_q_p6×now_prc_p4×std_exch_r = {stocks:.0f} but "
                           f"{dict(zip(OVERSEAS_TOTALS, totals))}; row units or grids differ from the spec")
    return stocks + cash


def _ledger_body(start, end):
    # the portal sample's full 19-key body; the page shows only strt_dt/end_dt/is_no/nxt_key/srt_clsf
    return {"inq_clsf": "1", **{f"inq_clsf{i}": "0" for i in range(1, 7)}, "strt_dt": f"{start:%Y%m%d}",
            "end_dt": f"{end:%Y%m%d}", "srt_clsf": "1", "nxt_key": "",
            **dict.fromkeys(("is_no", "strt_no", "dl_clsf", "dl_md_ccd", "md_isnc_tno", "crdt_crd_isnc_info",
                             "onl_prt_ccd", "isng_bl_at_trsns_xcl_f"), "")}


def _ledger(api, start, end):
    rows = []
    while start <= end:
        stop = min(end, start + timedelta(days=WINDOW - 1))
        rows += _rows(api.pages("SWQA2301", _ledger_body(start, stop)))
        start = stop + timedelta(days=1)
    return rows


def _transactions(rows, account_id, seeded):
    """seeded: the first row only sets the running balance; otherwise it starts from 0."""
    out, prev = [], None if seeded else Decimal(0)
    for i, r in enumerate(sorted(rows, key=lambda r: (r["dl_dt"], r["dl_sq"]))):
        d, sq, memo = r["dl_dt"].strip(), r["dl_sq"].strip(), r["smry_nm"].strip()
        if not str(r.get("tfnd_blnc") or "").strip():  # _num would read 0: a fake jump to an empty account
            raise RuntimeError(f"KB SWQA2301 {d}-{sq} {memo!r}: blank tfnd_blnc, no balance to sign the row by: {str(r)[:300]}")
        bal = _num(r, "tfnd_blnc")
        if prev is None:
            prev = bal
            continue
        amount, prev = bal - prev, bal
        t = {"account_id": account_id, "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "amount": float(amount), "memo": memo,
             "source_key": f"{d}-{sq}", "raw": r}
        kind, fx, ec = db.classify(memo, float(amount)), _num(r, "fcrncy_amt"), _num(r, "ec_amt")
        if i == 0 and abs(amount) != ec:  # the 0 opening balance does not fit (cash held before HISTORY_START)
            out.append(t | {"amount": 0.0, "kind": "INTERNAL" if kind == "INTERNAL" else "UNKNOWN"})  # sign unknown
            continue
        # trades may settle apart from their row; a foreign-cash-only row (fx, KRW unmoved) is handled below
        if kind != "INTERNAL" and abs(amount) != ec and (amount or not fx):
            raise RuntimeError(
                f"KB SWQA2301 {d}-{sq} {memo!r}: tfnd_blnc moved {bal - amount} -> {bal} but ec_amt={ec} "
                f"(dl_amt={r.get('dl_amt', '').strip()!r}, fcrncy_amt={fx}); the balance-delta sign rule does not hold here")
        if fx and not amount and kind != "INTERNAL":
            t["kind"] = "UNKNOWN"  # only foreign cash moved: its KRW value needs a human
        out.append(t)
    return out


def fetch(cfg, today, since):
    api = _Api(cfg)
    account_id = "kb:" + (cfg.get("KB_ACCOUNT_NO") or "main")  # the .env template ships the key blank
    value, cash = _domestic(api.pages("SSQM2952", DOMESTIC)[0])
    value += _overseas(api.pages("SPQM2226", OVERSEAS))
    if value <= 0:  # an empty-looking account is far likelier an API hiccup than real
        raise RuntimeError(f"KB: total value {value}, refusing a zero snapshot")
    # A rejected ledger row must not cost the day's valuation: past values can't be fetched again.
    txs, errors = [], []
    try:
        txs = _transactions(_ledger(api, since or HISTORY_START, today), account_id, seeded=since is not None)
    except RuntimeError as e:
        errors.append(f"SWQA2301: {e}")
    return {
        "accounts": [{"id": account_id, "institution": "kb", "name": cfg.get("KB_ACCOUNT_NAME") or "KB증권", "kind": "brokerage"}],
        "snapshots": [{"account_id": account_id, "date": today.isoformat(), "value": float(value), "cash": float(cash)}],
        "transactions": txs,
        "errors": errors,
    }


def _mask(o, secrets):
    if isinstance(o, dict):
        return {k: "***" if k in PII else _mask(v, secrets) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v, secrets) for v in o]
    if isinstance(o, str):
        for s in secrets:
            o = o.replace(s, "***")
    return o


def probe(cfg, days=WINDOW):
    """{TR: raw envelope | {"error": ...}}: first page of every TR fetch() calls, SWQA2301 over the last `days`.
    App key, secret, token and counterparty names (cprty_ac_nm) are masked."""
    secrets = [cfg["KB_APP_KEY"], cfg["KB_APP_SECRET"]]
    try:
        api = _Api(cfg)
    except Exception as e:
        return _mask({"token": {"error": f"{type(e).__name__}: {e}"}}, secrets)
    end, out = date.today(), {}
    for tr, body in (("SSQM2952", DOMESTIC), ("SPQM2226", OVERSEAS),
                     ("SWQA2301", _ledger_body(end - timedelta(days=days - 1), end))):
        try:
            out[tr] = api.raw(tr, body)
        except Exception as e:  # one failing call must not hide the others
            out[tr] = {"error": f"{type(e).__name__}: {e}"}
    return _mask(out, secrets + [api.token])


if __name__ == "__main__":
    import json
    print(json.dumps(probe(config.load()), ensure_ascii=False, indent=2))
