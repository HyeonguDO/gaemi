"""한국투자증권 KIS Developers REST: whole-account valuation snapshot (+ an opt-in, unpublished cash ledger).

Snapshot: 투자계좌자산현황조회 (CTRP6548R) output2.tot_asst_amt. The portal sample shows it is domestic stocks +
overseas stocks + FX cash + KRW deposit as one KRW figure. The alternative, inquire-balance tot_evlu_amt
(domestic + D+2 deposit) plus inquire-present-balance output3 (overseas), still misses FX cash and the scope of
output3.tot_asst_amt is unknown: that is where double counting creeps in. cash = output2.dncl_amt (KRW 예수금).

Transactions: KIS publishes no cash-flow ledger for stock accounts (the unpublished 계좌입출금고거래내역 CTRP6606R
answers EGW00206 to personal keys), so deposits and withdrawals come from the Excel/screen imports.
KIS_TRY_DEPOSIT_API=1 still tries CTRP6606R; probe() always tries it.
Overseas cash dividends are computed instead of read: 주당 배당금 (CTRGT011R) × shares held on its KR base date
(CTRP6010R, which answers for past dates), credited the day after the US pay date (HHDFS78330900). Checked
against 22 booked 해외증권배당금입금 rows (2026-01..02): every one matched to within a cent, i.e. KIS shows
the gross amount in USD. It then withholds 15% US tax in a line that list doesn't show: across 2026 the USD 예수금
grew by 85% of every dividend (cash reconciliation, see infer.py), and the 현지세금환급 rows refund exactly 15%.
So the ledger books the net (raw keeps gross and withheld). Converted to KRW at KIS's rate on the base date.
A day the dividends fail stores no cash legs either: infer.py would read an unsubtracted dividend as a deposit.

Unverified against a live account:
- tot_asst_amt is taken as gross, so value = tot_asst_amt - loan_amt_smtl (inferred from field names).
  Holdings and deposit should both be 결제기준 (settlement basis); compare once with HTS [0891], ideally on a
  day with trades. probe() also returns inquire-balance and inquire-present-balance for that cross-check.
- CTRP6606R: GET assumed; every request code except the dates is sent blank; tr_amt2 sign convention unknown
  (unsigned amounts take their sign from the memo, see OUT_WORDS); memo = acnt_syns_name + tr_itm_name;
  amounts assumed KRW; max date range unknown (the first sync asks from 2000-01-01); tr_dt:tr_seq assumed
  unique (a clash raises); an empty range is assumed to come back rt_cd "0". Dividends pass through row by row:
  a separate tax row nets into DIVIDEND only if its memo contains 배당 and it came out negative; any tax-looking
  row (세/징수) still positive, or classified as a plain deposit/withdrawal, goes to the review queue.
  Flows that also come from the Excel import would count twice (different source): use one or the other.
- Per-second limit: 20/s appears only in official repo comments. Calls are spaced 0.1 s and
  EGW00201/EGW00215 (rate limit) are retried twice after 1 s.
- Token rejection codes EGW00121/EGW00123 (invalid/expired) come from community reports, not the contract;
  on either the token is reissued once and the call retried.

Holdings (holdings(), the 보유 tab): overseas from CTRP6504R in 외화 mode (WCRC_FRCR_DVSN_CD 02). Checked live
(2026-09-25) against the same call in 원화 mode: avg_unpr3/ovrs_now_pric1/frcr_pchs_amt/frcr_evlu_amt2 come in
buy_crcy_cd, and × bass_exrt gives the 원화 mode's KRW figures to the won. frcr_evlu_amt2 = ccld_qty_smtl1 (체결기준)
× price, not cblc_qty13 (결제기준). A symbol held both normally and as 미니스탁 comes as two rows (prdt_dvsn 01/03);
they are merged. Domestic from TTTC8434R output1 (hldg_qty, pchs_amt, evlu_amt, prpr): contract fields only, the
live account held no domestic stock. The account total is _snapshot()'s, which is 결제기준, so an unsettled buy
sits in both the positions and the rest (현금·기타) until it settles.
KRW cost (cost_krw, so pnl_krw includes FX gains/losses) is pchs_rmnd_wcrc_amt, the KRW actually paid. It covers
the settled cblc_qty13 only: live (2026-09-25) its sum equals CTRP6548R's 해외주식 row pchs_amt, which KIS pairs
with a 결제기준 valuation (that day 3 of the JEPQ shares held were not settled yet). See _krw_cost() for the unsettled part.
"""
import hashlib
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .. import config, db

BASE = "https://openapi.koreainvestment.com:9443"
REQUIRED = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO")
GAP = 0.1                          # s between calls; the official samples' smart_sleep
BUSY = ("EGW00201", "EGW00215")    # per-second limit exceeded (personal / 원장-wide)
STALE = ("EGW00121", "EGW00123")  # invalid / expired token
MAX_PAGES = 100
KST = timezone(timedelta(hours=9))  # no DST in Korea
ACCOUNT_BALANCE = ("/uapi/domestic-stock/v1/trading/inquire-account-balance", "CTRP6548R")
DEPOSIT = ("/uapi/domestic-stock/v1/trading/inquire-deposit-withdrawal", "CTRP6606R")
OUT_WORDS = ("출금", "출고", "매수", "세금", "소득세", "수수료")  # money leaving the account
RIGHTS = ("/uapi/overseas-price/v1/quotations/period-rights", "CTRGT011R")        # per-share cash by record date
PAY_DATES = ("/uapi/overseas-price/v1/quotations/rights-by-ice", "HHDFS78330900")  # record_dt -> pay_dt (US)
HOLDINGS_AT = ("/uapi/overseas-stock/v1/trading/inquire-paymt-stdr-balance", "CTRP6010R")  # any past date
TRADES = ("/uapi/overseas-stock/v1/trading/inquire-period-trans", "CTOS4001R")
PRDT_TYPE = {"NASD": "512", "NAS": "512", "NYSE": "513", "NYS": "513", "AMEX": "529", "AMS": "529"}
EMPTY = ("KIER2620", "KIOK0560")  # 조회할 자료가 없습니다
HOLIDAYS = ("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R")  # 24 days a page from BASS_DT
RP_ROW, RP_ROWS = 7, 20  # CTRP6548R output1: 20 unlabelled asset-class rows; the 8th is RP/발행어음 (the USD auto-RP)
US_WITHHOLDING = 0.15     # treaty rate on US dividends, withheld by KIS at payment
INFER_DAYS = 40     # trades fetched for infer.py, which re-estimates the last few windows
RIGHTS_BACK = 31    # days before a dividend window that per-share rights (record dates) are looked up
BALANCE = ("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R")
BALANCE_Q = {"AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
             "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
PRESENT = ("/uapi/overseas-stock/v1/trading/inquire-present-balance", "CTRP6504R")


def _json(r, what):
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"KIS {what}: HTTP {r.status_code}, not JSON: {r.text[:200]!r}") from None


def _token(cfg, renew=False):
    def issue():
        r = requests.post(BASE + "/oauth2/tokenP", timeout=10, json={
            "grant_type": "client_credentials", "appkey": cfg["KIS_APP_KEY"], "appsecret": cfg["KIS_APP_SECRET"]})
        body = _json(r, "tokenP")
        if "access_token" not in body:  # error bodies carry error_code/error_description, never the key
            raise RuntimeError(f"KIS tokenP: HTTP {r.status_code} {body.get('error_code')} {body.get('error_description')}")
        life = float(body["expires_in"])
        if body.get("access_token_token_expired"):  # a re-request within 6 h hands back the older token: trust the end
            end = datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            life = min(life, end.timestamp() - time.time())
        return body["access_token"], life
    key = hashlib.sha256(cfg["KIS_APP_KEY"].encode()).hexdigest()[:8]  # a new app key never replays the old one's token
    return config.cached_token(f"kis:{key}", issue, margin=float("inf") if renew else 600)  # inf: skip the cache


def _get(cfg, path, tr_id, params, limit=None):
    """Body of every page; follows tr_cont F/M by echoing the body's ctx_area_* cursors back.
    `limit` stops after that many pages instead of failing (the holiday calendar never ends)."""
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {_token(cfg)}",
               "appkey": cfg["KIS_APP_KEY"], "appsecret": cfg["KIS_APP_SECRET"], "tr_id": tr_id,
               "custtype": "P", "tr_cont": ""}
    pages, renewed = [], False
    while len(pages) < (limit or MAX_PAGES):
        for wait in (GAP, 1, 1):
            time.sleep(wait)
            r = requests.get(BASE + path, headers=headers, params=params, timeout=10)
            body = _json(r, tr_id)
            if body.get("msg_cd") not in BUSY:
                break
        if body.get("msg_cd") in STALE and not renewed:  # cached token dropped server-side: reissue once, redo the page
            headers["authorization"], renewed = f"Bearer {_token(cfg, renew=True)}", True
            continue
        if r.status_code != 200 or body.get("rt_cd") != "0":
            raise RuntimeError(f"KIS {tr_id}: HTTP {r.status_code} [{body.get('msg_cd')}] {body.get('msg1')}")
        pages.append(body)
        if r.headers.get("tr_cont") not in ("F", "M"):
            return pages
        headers["tr_cont"] = "N"
        params = {**params, **{k.upper(): v for k, v in body.items() if k.startswith("ctx_area_")}}
    if limit:
        return pages
    raise RuntimeError(f"KIS {tr_id}: more than {MAX_PAGES} pages")


def _account(cfg):
    return {"CANO": cfg["KIS_CANO"], "ACNT_PRDT_CD": cfg.get("KIS_ACNT_PRDT_CD") or "01"}


def _snapshot(cfg):
    """(value, KRW 예수금, KRW in RP/발행어음 or None when output1 isn't the verified 20-row layout)."""
    body = _get(cfg, *ACCOUNT_BALANCE, {**_account(cfg), "INQR_DVSN_1": "", "BSPR_BF_DT_APLY_YN": ""})[0]
    o2, o1 = body.get("output2"), body.get("output1") or []
    o2 = o2[0] if isinstance(o2, list) and o2 else o2  # spec: an object; tolerate a one-row array
    try:
        total, loans, cash = (float(o2[k]) for k in ("tot_asst_amt", "loan_amt_smtl", "dncl_amt"))
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"KIS CTRP6548R: need output2 tot_asst_amt/loan_amt_smtl/dncl_amt, got {str(o2)[:300]}") from None
    if total <= 0:  # an empty-looking account is far likelier an API hiccup than real; never record it silently
        raise RuntimeError(f"KIS CTRP6548R: tot_asst_amt={o2['tot_asst_amt']!r}, refusing a zero snapshot: {str(o2)[:300]}")
    rp = float(o1[RP_ROW].get("pchs_amt") or 0) if len(o1) == RP_ROWS and isinstance(o1[RP_ROW], dict) else None
    return total - loans, cash, rp


def _business_days(cfg, start, end, flag="sttl_day_yn"):
    """KR settlement days (sttl_day_yn) in [start, end] from CTCA0903R; plain weekdays where it has no answer.
    Not bzdy_yn: 12-31 and 05-01 are 영업일 but no 결제일, and the recordings show no settlement on them."""
    known = {}
    try:
        pages = _get(cfg, *HOLIDAYS, {"BASS_DT": f"{start:%Y%m%d}", "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                     limit=(end - start).days // 24 + 2)
        known = {_ymd(x["bass_dt"]): x[flag] == "Y" for p in pages for x in p.get("output") or []}
    except RuntimeError:  # KIS asks to call it sparingly; a weekday guess only blurs a window by a day
        pass
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [d for d in days if known.get(d, d.weekday() < 5)]


def _cash_legs(cfg, today, rp_krw):
    """Today's settlement-basis KRW and USD 예수금 (CTRP6010R) and the RP, for infer.py, and the rate the day's
    snapshot was priced at; None without data."""
    try:
        body = _get(cfg, *HOLDINGS_AT, {**_account(cfg), "BASS_DT": f"{today:%Y%m%d}", "WCRC_FRCR_DVSN_CD": "01",
                                        "INQR_DVSN_CD": "00"})[0]
    except RuntimeError as e:
        if any(code in str(e) for code in EMPTY):
            return None
        raise
    o3 = body["output3"][0] if isinstance(body["output3"], list) else body["output3"]
    usd = [x for x in body.get("output2") or [] if x.get("crcy_cd") == "USD"]
    rate = next((float(x["frst_bltn_exrt"]) for x in usd if float(x.get("frst_bltn_exrt") or 0) > 0), None) or next(
        (float(x["bass_exrt"]) for x in body.get("output1") or [] if float(x.get("bass_exrt") or 0) > 0), None)
    if not rate:
        return None
    return {"krw": float(o3["tot_dncl_amt"]), "usd": sum(float(x["frcr_dncl_amt_2"]) for x in usd),
            "rp_usd": rp_krw / rate, "rate": rate}


def _settlements(cfg, account_id, start, today):
    """Cash moved by overseas trades, by the KR day it moved. sttl_dt is the US settlement date and KIS moves the
    money the next KR settlement morning (2026 screen recordings). 미니스탁 trades settle in KRW (wcrc_excc_amt) with
    the fee in KRW (dmst_wcrc_fee); regular ones in USD, fee included (frcr_excc_amt_1 = tr_frcr_amt2 ± fees on
    every regular trade since 2025-12)."""
    rows = _rows(cfg, TRADES, {**_account(cfg), "ERLM_STRT_DT": f"{start:%Y%m%d}", "ERLM_END_DT": f"{today:%Y%m%d}",
                               "OVRS_EXCG_CD": "", "PDNO": "", "SLL_BUY_DVSN_CD": "00", "LOAN_DVSN_CD": "",
                               "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}, "output1")
    biz = _business_days(cfg, start, today + timedelta(days=10)) if rows else []
    out = {}
    for x in rows:
        sttl = _ymd(x["sttl_dt"])
        day = next((d for d in biz if d > sttl), sttl + timedelta(days=1)).isoformat()
        leg = out.setdefault(day, {"account_id": account_id, "date": day, "usd": 0.0, "krw": 0.0})
        sign = 1 if x["sll_buy_dvsn_cd"] == "01" else -1
        if x.get("loan_dvsn_name") == "미니스탁":
            leg["krw"] += sign * float(x["wcrc_excc_amt"]) - float(x.get("dmst_wcrc_fee") or 0)
        else:
            leg["usd"] += sign * float(x["frcr_excc_amt_1"])
    return [out[k] for k in sorted(out)]


def _rows(cfg, api, params, key):
    try:
        return [x for page in _get(cfg, *api, params) for x in (page.get(key) or [])]
    except RuntimeError as e:
        if any(code in str(e) for code in EMPTY):
            return []
        raise


def _ymd(s):
    return datetime.strptime(s, "%Y%m%d").date()


def _credit_day(d):
    # ponytail: weekends and 1/1 only; other KR holidays shift a credit by a day or two, which the yearly math can absorb
    while d.weekday() >= 5 or (d.month, d.day) == (1, 1):
        d += timedelta(days=1)
    return d


def _dividends(cfg, account_id, start, today):
    """Overseas cash dividends credited in (start, today], one row per symbol and record date."""
    acc, held = _account(cfg), {}

    def holdings(bass):  # (rows, {pdno: shares incl. 미니스탁}, KRW per USD) at the close of a KR date
        if bass not in held:
            rows = _rows(cfg, HOLDINGS_AT, {**acc, "BASS_DT": bass, "WCRC_FRCR_DVSN_CD": "02", "INQR_DVSN_CD": "00"},
                         "output1")
            shares = {}
            for x in rows:
                shares[x["pdno"]] = shares.get(x["pdno"], 0.0) + float(x["cblc_qty13"])
            rate = next((float(x["bass_exrt"]) for x in rows if float(x.get("bass_exrt") or 0) > 0), None)
            held[bass] = rows, shares, rate
        return held[bass]

    # Anything that could have paid in the window: held now, or traded since the earliest record date priced (a
    # position sold after its record date but before `start` still gets that dividend).
    exchange = {x["pdno"]: x.get("ovrs_excg_cd") for x in holdings(f"{today:%Y%m%d}")[0]}
    for x in _rows(cfg, TRADES, {**acc, "ERLM_STRT_DT": f"{start - timedelta(days=RIGHTS_BACK):%Y%m%d}",
                                 "ERLM_END_DT": f"{today:%Y%m%d}",
                                 "OVRS_EXCG_CD": "", "PDNO": "", "SLL_BUY_DVSN_CD": "00", "LOAN_DVSN_CD": "",
                                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}, "output1"):
        exchange.setdefault(x["pdno"], x.get("ovrs_excg_cd"))
    return us_dividends(cfg, account_id, exchange, start, today,
                        shares_at=lambda pdno, record, bass: holdings(bass)[1].get(pdno, 0.0),
                        rate_at=lambda bass: holdings(bass)[2])


def rights_of(cfg, pdno, excg, start, end):
    """CTRGT011R cash rights (alct_frcr_unpr: USD per share, gross) of one US symbol with a US record date in
    [start, end]. Without an exchange code (Toss holdings carry none) the product types are tried in turn."""
    for kind in [PRDT_TYPE[excg]] if excg in PRDT_TYPE else ["512", "529", "513"]:
        rights = [x for x in _rows(cfg, RIGHTS, {
            "RGHT_TYPE_CD": "03", "INQR_DVSN_CD": "02", "INQR_STRT_DT": f"{start:%Y%m%d}",
            "INQR_END_DT": f"{end:%Y%m%d}", "PDNO": pdno, "PRDT_TYPE_CD": kind,
            "CTX_AREA_NK50": "", "CTX_AREA_FK50": ""}, "output")
            if x["pdno"] == pdno and float(x.get("alct_frcr_unpr") or 0) > 0]  # PDNO is a prefix match (VOO: VOOG)
        if rights:
            return rights
    return []


def us_dividends(cfg, account_id, symbols, start, today, shares_at, rate_at, credit_lag=1):
    """US cash dividends credited in (start, today] for {pdno: exchange code or None}, as ledger rows, net of the 15%
    withholding. Per-share amounts come from KIS (CTRGT011R, pay dates HHDFS78330900) whoever holds the shares:
    shares_at(pdno, US record date, KR base date) and rate_at(KR base date) come from the holding broker.
    KIS credits the day after the US pay date (credit_lag=1); Toss on the pay date itself (6 of its 7 in 2026,
    credit_lag=0)."""
    out = []
    for pdno, excg in sorted(symbols.items()):
        rights = rights_of(cfg, pdno, excg, start - timedelta(days=RIGHTS_BACK), today)
        if not rights:
            continue
        pay = {x["record_dt"]: x["pay_dt"] for x in _rows(cfg, PAY_DATES, {
            "NCOD": "US", "SYMB": pdno, "ST_YMD": f"{start - timedelta(days=120):%Y%m%d}", "ED_YMD": f"{today:%Y%m%d}"},
            "output1") if x.get("ca_title") == "현금배당" and x.get("pay_dt")}  # filtered by announcement date
        for x in rights:
            p = pay.get(x["acpl_bass_dt"])
            credited = _credit_day(_ymd(p) + timedelta(days=credit_lag) if p else _ymd(x["bass_dt"]) + timedelta(days=2))
            if not start < credited <= today:  # outside the window, or announced but not paid yet
                continue
            shares = shares_at(pdno, x["acpl_bass_dt"], x["bass_dt"])
            gross = round(shares * float(x["alct_frcr_unpr"]), 2)
            withheld = round(gross * US_WITHHOLDING, 2)
            usd = round(gross - withheld, 2)
            if usd <= 0:
                continue
            rate = rate_at(x["bass_dt"])
            if not rate:
                raise RuntimeError(f"KIS CTRP6010R {x['bass_dt']}: no bass_exrt to convert {pdno} dividend {usd} USD")
            out.append({"account_id": account_id, "date": credited, "amount": round(usd * rate),
                        "memo": f"해외배당 {pdno}", "source_key": f"div:{pdno}:{x['acpl_bass_dt']}",
                        "raw": {"usd": usd, "gross": gross, "withheld": withheld,
                                "per_share": float(x["alct_frcr_unpr"]), "shares": shares,
                                "record_dt": x["acpl_bass_dt"], "kr_base_dt": x["bass_dt"], "pay_dt": p, "rate": rate}})
    return out


def _deposit_params(cfg, start, end):
    blank = ("ACNT_TR_TYPE_CD", "PRDT_CTRT_NO", "PDNO", "PRDT_TYPE_CD", "CMA_AUTO_TR_YN", "PWD_CHK_YN", "INQR_DVSN",
             "EXONO", "RCTM_BANO", "RCTM_BANK_ACNT_NAME", "RNWL_DFPA_ECLS_YN", "ATN_TR_STTL_ECLS_YN",
             "CTX_AREA_NK30", "CTX_AREA_FK100")
    return {**_account(cfg), "INQR_STRT_DT": f"{start:%Y%m%d}", "INQR_END_DT": f"{end:%Y%m%d}", **dict.fromkeys(blank, "")}


def _transactions(pages, account_id):
    out, seen = [], {}
    for page in pages:
        if not isinstance(page.get("output1"), list):
            raise RuntimeError(f"KIS CTRP6606R: expected an output1 list, got keys {sorted(page)}")
        for row in page["output1"]:
            try:
                d, seq, amount = row["tr_dt"], row["tr_seq"], float(row["tr_amt2"])
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(f"KIS CTRP6606R: need tr_dt/tr_seq/tr_amt2, got {str(row)[:300]}") from None
            key = f"{d}:{seq}"
            if key in seen:  # upsert would silently keep only the last of two rows sharing a source_key
                if seen[key] == row:
                    continue  # the same row served again on an overlapping page
                raise RuntimeError(f"KIS CTRP6606R: tr_dt:tr_seq {key} is not unique: {str(seen[key])[:300]} vs {str(row)[:300]}")
            seen[key] = row
            memo = " ".join(filter(None, (str(row.get(k) or "").strip() for k in ("acnt_syns_name", "tr_itm_name"))))
            if amount > 0 and "입금" not in memo and any(w in memo for w in OUT_WORDS):
                amount = -amount  # ponytail: sign convention unknown; an unsigned outflow gets it from the memo
            t = {"account_id": account_id, "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "amount": amount, "memo": memo,
                 "source_key": key, "raw": row}
            if row.get("cncl_yn") == "Y":
                t["kind"] = "INTERNAL"  # cancelled: keep the row (an earlier sync may have stored it) but out of metrics
            elif ("세" in memo or "징수" in memo) and (amount > 0 or db.classify(memo, amount) in ("DEPOSIT", "WITHDRAW")):
                t["kind"] = "UNKNOWN"   # an unsigned tax row must not add to dividends, nor pass for a withdrawal
            out.append(t)
    return out


def fetch(cfg, today, since):
    acc = _account(cfg)
    account_id = f"kis:{acc['CANO']}-{acc['ACNT_PRDT_CD']}"
    value, cash, rp_krw = _snapshot(cfg)
    txs, errors, cash_obs, settlements, ledger_api, dividends_ok = [], [], [], [], False, True
    try:  # first sync: this year so far; later: sync's refetch window
        txs += _dividends(cfg, account_id, since - timedelta(days=1) if since else date(today.year - 1, 12, 31), today)
    except Exception as e:  # dividends are recomputable later; the day's valuation is not
        errors.append(f"dividends (today's cash legs skipped too): {type(e).__name__}: {e}")
        dividends_ok = False
    if cfg.get("KIS_TRY_DEPOSIT_API") == "1":
        try:  # unpublished API: its failure must not cost the day's valuation
            txs += _transactions(_get(cfg, *DEPOSIT, _deposit_params(cfg, since or date(2000, 1, 1), today)), account_id)
            ledger_api = True  # booked flows from KIS itself: nothing to infer
        except Exception as e:
            errors.append(f"CTRP6606R: {type(e).__name__}: {e}")
    legs = None
    try:  # its rate prices the snapshot in USD (metrics.in_usd); storing the legs for infer.py stays optional
        legs = _cash_legs(cfg, today, rp_krw or 0.0)
        if legs and not ledger_api and dividends_ok:
            if rp_krw is None:
                raise RuntimeError("CTRP6548R output1 is no longer the 20-row layout; the RP leg is unknown")
            cash_obs.append({"account_id": account_id, "date": today.isoformat(), **legs})
            settlements = _settlements(cfg, account_id, today - timedelta(days=INFER_DAYS), today)
    except Exception as e:
        cash_obs, settlements = [], []
        errors.append(f"cash legs: {type(e).__name__}: {e}")
    return {
        "accounts": [{"id": account_id, "institution": "kis", "name": cfg.get("KIS_ACCOUNT_NAME") or "한국투자증권",
                      "kind": "brokerage"}],
        "snapshots": [{"account_id": account_id, "date": today.isoformat(), "value": value, "cash": cash,
                       "rate": legs and legs["rate"]}],  # CTRP6548R prices tot_asst_amt at the same rate
        # the day's rate for flows too if Toss's sync fails (sync runs toss after kis: its midRate still wins)
        "fx": [{"date": today.isoformat(), "currency": "USD", "krw": legs["rate"]}] if legs else [],
        "transactions": txs,
        "errors": errors,
        "cash_obs": cash_obs,
        "settlements": settlements,
        "settlements_from": (today - timedelta(days=INFER_DAYS)).isoformat(),
    }


def _krw_cost(x):
    """KRW cost of a CTRP6504R row's ccld_qty_smtl1 (체결기준) shares. pchs_rmnd_wcrc_amt covers the settled
    cblc_qty13: an unsettled sell drops its average share of it, an unsettled buy has no KRW cost yet."""
    held, settled = float(x["ccld_qty_smtl1"]), float(x["cblc_qty13"])
    cost = float(x["pchs_rmnd_wcrc_amt"]) * min(held, settled) / settled if settled > 0 else 0.0
    # ponytail: an unsettled buy (1-2 days old) is taken at today's value, i.e. zero P&L until KIS books its KRW cost
    return cost + max(held - settled, 0.0) * float(x["ovrs_now_pric1"]) * float(x["bass_exrt"])


def holdings(cfg):
    """[{account_id, total_krw, positions}] for the one KIS account; prices in the row's own currency, P&L in KRW."""
    acc = _account(cfg)
    total, _, _ = _snapshot(cfg)
    rows = [(x["pdno"], x["prdt_name"], "KRX", "KRW", 1.0, float(x["hldg_qty"]), float(x["pchs_amt"]),
             float(x["evlu_amt"]), float(x["prpr"]), float(x["pchs_amt"]))
            for x in _rows(cfg, BALANCE, {**acc, **BALANCE_Q}, "output1")]
    rows += [(x["pdno"], x["prdt_name"], x["ovrs_excg_cd"], x["buy_crcy_cd"], float(x["bass_exrt"]),
              float(x["ccld_qty_smtl1"]), float(x["frcr_pchs_amt"]), float(x["frcr_evlu_amt2"]),
              float(x["ovrs_now_pric1"]), _krw_cost(x))
             for x in _rows(cfg, PRESENT, {**acc, "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "000", "TR_MKET_CD": "00",
                                           "INQR_DVSN_CD": "00"}, "output1")]
    pos = {}
    for symbol, name, market, currency, fx, qty, cost, value, price, cost_krw in rows:
        if qty <= 0:  # sold today: the row lingers with nothing held
            continue
        p = pos.setdefault((symbol, currency), {"symbol": symbol, "name": name, "market": market, "currency": currency,
                                                "quantity": 0.0, "price": price, "cost": 0.0, "value": 0.0, "fx": fx,
                                                "cost_krw": 0.0})
        for k, v in (("quantity", qty), ("cost", cost), ("value", value), ("cost_krw", cost_krw)):  # 미니스탁 + regular
            p[k] += v
    out = []
    for p in pos.values():
        cost, value, fx = p.pop("cost"), p.pop("value"), p.pop("fx")
        pnl = value * fx - p["cost_krw"]  # FX gains/losses included: cost is the KRW paid
        out.append({**p, "avg_price": cost / p["quantity"], "value_krw": value * fx, "pnl_krw": pnl,
                    "pnl_rate": pnl / p["cost_krw"] if p["cost_krw"] else None, "pnl_ex_fx": False,
                    # in the row's currency, FX left out: pnl_krw = pnl_local*fx + (cost_local*fx - cost_krw)
                    "cost_local": cost, "pnl_local": value - cost, "pnl_local_rate": (value - cost) / cost if cost else None})
    return [{"account_id": f"kis:{acc['CANO']}-{acc['ACNT_PRDT_CD']}", "total_krw": total,
             "positions": sorted(out, key=lambda p: -p["value_krw"])}]


def _mask(o, secrets):
    if isinstance(o, dict):
        return {k: _mask(v, secrets) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v, secrets) for v in o]
    if isinstance(o, str):
        for s in secrets:
            o = o.replace(s, "***")
    return o


def probe(cfg):
    """{call: [page bodies] | {"error": ...}} for every call worth checking on a live account.
    Keys, token and the account number are masked."""
    today, acc, token = date.today(), _account(cfg), _token(cfg)
    calls = {
        "inquire-account-balance": (*ACCOUNT_BALANCE, {**acc, "INQR_DVSN_1": "", "BSPR_BF_DT_APLY_YN": ""}),
        "inquire-balance": (*BALANCE, {**acc, **BALANCE_Q}),
        "inquire-present-balance": (*PRESENT, {
            **acc, "WCRC_FRCR_DVSN_CD": "01", "NATN_CD": "000", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"}),
        "period-rights": ("/uapi/domestic-stock/v1/trading/period-rights", "CTRGA011R", {
            **acc, "INQR_DVSN": "03", "CUST_RNCNO25": "", "HMID": "", "INQR_STRT_DT": f"{today - timedelta(days=365):%Y%m%d}",
            "INQR_END_DT": f"{today:%Y%m%d}", "RGHT_TYPE_CD": "", "PDNO": "", "PRDT_TYPE_CD": "",
            "CTX_AREA_NK100": "", "CTX_AREA_FK100": ""}),
        "inquire-deposit-withdrawal": (*DEPOSIT, _deposit_params(cfg, today - timedelta(days=90), today)),
    }
    out = {}
    for name, (path, tr_id, params) in calls.items():
        try:
            out[name] = _get(cfg, path, tr_id, params)
        except Exception as e:  # one failing call must not hide the others
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    return _mask(out, (cfg["KIS_APP_KEY"], cfg["KIS_APP_SECRET"], token, _token(cfg), cfg["KIS_CANO"]))  # + a reissued one
