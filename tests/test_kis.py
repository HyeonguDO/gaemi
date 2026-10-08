import json
import time
from datetime import date, datetime, timedelta

import pytest

from assetlog import config, db
from assetlog.brokers import kis

CFG = {"KIS_APP_KEY": "A" * 36, "KIS_APP_SECRET": "S" * 180, "KIS_CANO": "12345678"}
TOKEN = "T" * 350



def expiry(hours):  # access_token_token_expired, KST wall clock
    return (datetime.now(kis.KST) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# Portal samples (docs/contracts/kis.json). loan_amt_smtl is a documented output2 field missing from the excerpt.
# The sample's 2023 expiry is moved into the future: the cache trusts it over expires_in.
TOKEN_BODY = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400, "access_token_token_expired": expiry(24)}
ACCOUNT_BALANCE = {"rt_cd": "0", "msg_cd": "KIOK0510", "msg1": "조회가 완료되었습니다", "output1": [], "output2": {
    "evlu_amt_smtl": "185550504", "nass_tot_amt": "185550504", "tot_asst_amt": "1651869889547",
    "frcr_evlu_tota": "1651434483743", "dncl_amt": "249855300", "ovrs_stck_evlu_amt1": "185144504.000000",
    "loan_amt_smtl": "0"}}
PERIOD_RIGHTS = {"rt_cd": "0", "msg1": "조회 되었습니다. (마지막 자료)", "output": [
    {"acno10": "1234567801", "rght_type_cd": "01", "bass_dt": "20240919", "rght_cblc_type_cd": "01",
     "shtn_pdno": "357880", "cblc_qty": "1000", "last_alct_amt": "0", "cash_dfrm_dt": "", "tax_amt": "0",
     "rqst_amt": "1865000"}]}


class Resp:
    def __init__(self, body, tr_cont="D", status=200):
        self.body, self.status_code, self.headers = body, status, {"tr_cont": tr_cont}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self.body


DIVIDEND_TRS = {kis.HOLDINGS_AT[1], kis.TRADES[1], kis.RIGHTS[1], kis.PAY_DATES[1]}


@pytest.fixture
def api(monkeypatch, tmp_path):
    """routes: tr_id -> responses served in order; gets: (tr_id, headers, params) per call."""
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(kis.time, "sleep", lambda s: None)
    fake = type("Api", (), {"routes": {}, "gets": [], "posts": 0, "token": Resp(TOKEN_BODY)})()

    def post(url, json, timeout):
        assert url.endswith("/oauth2/tokenP") and json["appkey"] in (CFG["KIS_APP_KEY"], "B" * 36)
        fake.posts += 1
        return fake.token

    def get(url, headers, params, timeout):
        fake.gets.append((headers["tr_id"], dict(headers), dict(params)))
        tr = headers["tr_id"]
        if not fake.routes.get(tr) and tr in DIVIDEND_TRS:  # tests about other things: no dividends anywhere
            return Resp({"rt_cd": "1", "msg_cd": "KIER2620", "msg1": "조회할 자료가 없습니다"}, status=200)
        return fake.routes[tr].pop(0)

    monkeypatch.setattr(kis.requests, "post", post)
    monkeypatch.setattr(kis.requests, "get", get)
    return fake


def test_snapshot_is_whole_account_total_and_token_is_cached(api):
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE), Resp(ACCOUNT_BALANCE)]
    out = kis.fetch(CFG, date(2026, 9, 25), None)
    assert out["accounts"] == [{"id": "kis:12345678-01", "institution": "kis", "name": "한국투자증권", "kind": "brokerage"}]
    assert out["snapshots"] == [{"account_id": "kis:12345678-01", "date": "2026-09-25",
                                 "value": 1651869889547.0, "cash": 249855300.0,
                                 "rate": None}]  # CTRP6010R came back empty: dollar figures use the day's fx
    assert out["transactions"] == []  # deposit API is opt-in
    tr_id, headers, params = api.gets[0]
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["custtype"] == "P" and headers["tr_cont"] == ""
    assert params["CANO"] == "12345678" and params["ACNT_PRDT_CD"] == "01"
    kis.fetch(CFG, date(2026, 9, 25), None)
    assert api.posts == 1 and [g[0] for g in api.gets if g[0] not in DIVIDEND_TRS] == ["CTRP6548R", "CTRP6548R"]
    assert "errors" not in out or not out["errors"]  # "no dividends" is not an error


def test_loans_are_subtracted(api):
    body = json.loads(json.dumps(ACCOUNT_BALANCE))
    body["output2"]["loan_amt_smtl"] = "1000000"
    api.routes["CTRP6548R"] = [Resp(body)]
    assert kis.fetch(CFG, date(2026, 9, 25), None)["snapshots"][0]["value"] == 1651869889547 - 1000000


def test_deposit_api_pages_and_signs(api):
    def row(dt, seq, memo, amt, cncl="N"):
        return {"tr_dt": dt, "tr_seq": seq, "cncl_yn": cncl, "acnt_syns_name": memo, "tr_itm_name": "", "tr_amt2": amt}
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE)]
    api.routes["CTRP6606R"] = [
        Resp({"rt_cd": "0", "ctx_area_nk30": "NK1", "ctx_area_fk100": "FK1", "output1": [
            row("20260903", "1", "이체입금", "1000000"), row("20260910", "2", "이체출금", "300000")]}, tr_cont="M"),
        Resp({"rt_cd": "0", "ctx_area_nk30": "", "ctx_area_fk100": "", "output1": [
            row("20260915", "3", "배당금입금", "50000"), row("20260915", "4", "배당소득세", "7700"),
            row("20260916", "5", "원천세출금", "100"), row("20260917", "6", "이체입금", "5000", cncl="Y"),
            row("20260918", "7", "배당원천세", "7700"), row("20260918", "8", "배당금원천징수", "7700"),
            row("20260918", "8", "배당금원천징수", "7700")]}),  # repeated verbatim: skipped
    ]
    out = kis.fetch({**CFG, "KIS_TRY_DEPOSIT_API": "1"}, date(2026, 9, 25), date(2026, 9, 1))
    first, second = [g for g in api.gets if g[0] == "CTRP6606R"]
    assert first[2]["INQR_STRT_DT"] == "20260901" and first[2]["INQR_END_DT"] == "20260925"
    assert second[1]["tr_cont"] == "N" and (second[2]["CTX_AREA_NK30"], second[2]["CTX_AREA_FK100"]) == ("NK1", "FK1")
    got = [(t["date"], t["amount"], t.get("kind") or db.classify(t["memo"], t["amount"]), t["source_key"])
           for t in out["transactions"]]
    assert got == [
        ("2026-09-03", 1000000, "DEPOSIT", "20260903:1"),
        ("2026-09-10", -300000, "WITHDRAW", "20260910:2"),
        ("2026-09-15", 50000, "DIVIDEND", "20260915:3"),
        ("2026-09-15", -7700, "DIVIDEND", "20260915:4"),   # tax nets into the dividend
        ("2026-09-16", -100, "UNKNOWN", "20260916:5"),     # tax without 배당/이자: not a withdrawal
        ("2026-09-17", 5000, "INTERNAL", "20260917:6"),    # cancelled
        ("2026-09-18", 7700, "UNKNOWN", "20260918:7"),     # unsigned tax: never added to dividends
        ("2026-09-18", 7700, "UNKNOWN", "20260918:8"),
    ]


def test_deposit_api_key_clash_fails_loudly(api):
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE)]
    api.routes["CTRP6606R"] = [Resp({"rt_cd": "0", "output1": [
        {"tr_dt": "20260915", "tr_seq": "7", "acnt_syns_name": "배당금입금", "tr_amt2": "50000"},
        {"tr_dt": "20260915", "tr_seq": "7", "acnt_syns_name": "배당원천세", "tr_amt2": "-7700"}]})]
    out = kis.fetch({**CFG, "KIS_TRY_DEPOSIT_API": "1"}, date(2026, 9, 25), None)
    assert out["transactions"] == [] and "20260915:7 is not unique" in out["errors"][0]
    assert len(out["snapshots"]) == 1  # the day's valuation survives a rejected ledger


def test_token_cache_uses_absolute_expiry_and_app_key(api):
    api.token = Resp({**TOKEN_BODY, "access_token_token_expired": expiry(3)})  # older token handed back, 3 h left
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE), Resp(ACCOUNT_BALANCE)]
    kis.fetch(CFG, date(2026, 9, 25), None)
    (name, hit), = json.loads((config.DATA / "tokens.json").read_text()).items()
    assert name.startswith("kis:") and CFG["KIS_APP_KEY"] not in name
    assert abs(hit["expires"] - (time.time() + 3 * 3600)) < 60
    kis.fetch({**CFG, "KIS_APP_KEY": "B" * 36}, date(2026, 9, 25), None)
    assert api.posts == 2  # another app key never reuses the first key's token


def test_rejected_token_is_reissued_once(api):
    stale = Resp({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}, status=500)
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE), stale, Resp(ACCOUNT_BALANCE), stale, stale]
    kis.fetch(CFG, date(2026, 9, 25), None)
    api.token = Resp({**TOKEN_BODY, "access_token": "U" * 350})
    assert kis.fetch(CFG, date(2026, 9, 25), None)["snapshots"][0]["value"] == 1651869889547
    assert api.posts == 2 and api.gets[-1][1]["authorization"] == "Bearer " + "U" * 350
    with pytest.raises(RuntimeError, match="EGW00123"):  # the fresh token rejected too: no second reissue
        kis.fetch(CFG, date(2026, 9, 25), None)
    assert api.posts == 3


def test_rate_limit_is_retried(api):
    busy = Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}, status=500)
    api.routes["CTRP6548R"] = [busy, Resp(ACCOUNT_BALANCE)]
    assert kis.fetch(CFG, date(2026, 9, 25), None)["snapshots"][0]["value"] == 1651869889547


@pytest.mark.parametrize("body, match", [
    ({"rt_cd": "0", "output2": {"dncl_amt": "1"}}, "tot_asst_amt"),
    ({"rt_cd": "0", "output2": {**ACCOUNT_BALANCE["output2"], "tot_asst_amt": "0"}}, "zero snapshot"),
    ({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 서비스 코드 입니다"}, "OPSQ0002"),
])
def test_bad_balance_fails_loudly(api, body, match):
    api.routes["CTRP6548R"] = [Resp(body)]
    with pytest.raises(RuntimeError, match=match):
        kis.fetch(CFG, date(2026, 9, 25), None)


def test_token_error_names_code_not_secret(api):
    api.token = Resp({"error_code": "EGW00133", "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"}, status=403)
    with pytest.raises(RuntimeError, match="EGW00133") as e:
        kis.fetch(CFG, date(2026, 9, 25), None)
    assert CFG["KIS_APP_SECRET"] not in str(e.value)


def test_probe_masks_and_keeps_going(api):
    api.routes = {"CTRP6548R": [Resp(ACCOUNT_BALANCE)], "TTTC8434R": [Resp({"rt_cd": "0", "output1": [], "output2": [{}]})],
                  "CTRP6504R": [Resp({"rt_cd": "0", "output1": [], "output2": [], "output3": {}})],
                  "CTRGA011R": [Resp(PERIOD_RIGHTS)],
                  "CTRP6606R": [Resp({"rt_cd": "1", "msg_cd": "EGW00203", "msg1": f"bad {CFG['KIS_APP_KEY']}"}, status=500)]}
    out = kis.probe(CFG)
    assert out["period-rights"][0]["output"][0]["acno10"] == "***01"
    assert out["inquire-deposit-withdrawal"]["error"].startswith("RuntimeError: KIS CTRP6606R: HTTP 500 [EGW00203]")
    dump = json.dumps(out, ensure_ascii=False)
    assert all(s not in dump for s in (CFG["KIS_APP_KEY"], CFG["KIS_APP_SECRET"], TOKEN, CFG["KIS_CANO"]))
    assert out["inquire-account-balance"][0]["output2"]["tot_asst_amt"] == "1651869889547"


def test_overseas_dividend_is_per_share_times_shares_on_base_date(api):
    ok = lambda key, rows: Resp({"rt_cd": "0", "msg_cd": "KIOK0000", key: rows})
    api.routes[kis.HOLDINGS_AT[1]] = [
        ok("output1", [{"pdno": "JEPQ", "cblc_qty13": "120", "ovrs_excg_cd": "NASD", "bass_exrt": "1360"}]),  # today
        ok("output1", [{"pdno": "JEPQ", "cblc_qty13": "87", "bass_exrt": "1451.2"},                           # 02-03
                       {"pdno": "JEPQ", "cblc_qty13": "1", "bass_exrt": "1451.2"}]),                          # 미니스탁
    ]
    api.routes[kis.RIGHTS[1]] = [ok("output", [
        {"pdno": "JEPQ", "acpl_bass_dt": "20260202", "bass_dt": "20260203", "alct_frcr_unpr": "0.46572"},
        {"pdno": "JEPQX", "acpl_bass_dt": "20260202", "bass_dt": "20260203", "alct_frcr_unpr": "9"},  # prefix match
        {"pdno": "JEPQ", "acpl_bass_dt": "20260930", "bass_dt": "20261001", "alct_frcr_unpr": "0.5"},  # not paid yet
    ])]
    api.routes[kis.PAY_DATES[1]] = [ok("output1", [
        {"ca_title": "현금배당", "record_dt": "20260202", "pay_dt": "20260204"},
        {"ca_title": "현금배당", "record_dt": "20260930", "pay_dt": "20261002"},
    ])]
    out = kis._dividends(CFG, "kis:x", date(2025, 12, 31), date(2026, 9, 25))
    assert out == [{"account_id": "kis:x", "date": date(2026, 2, 5), "amount": round(34.83 * 1451.2),  # net of 15%
                    "memo": "해외배당 JEPQ", "source_key": "div:JEPQ:20260202", "raw": {
                        "usd": 34.83, "gross": 40.98, "withheld": 6.15,
                        "per_share": 0.46572, "shares": 88.0, "record_dt": "20260202",
                        "kr_base_dt": "20260203", "pay_dt": "20260204", "rate": 1451.2}}]
    assert db.classify(out[0]["memo"], out[0]["amount"]) == "DIVIDEND"


def test_dividend_of_a_position_sold_after_its_record_date_but_before_the_window(monkeypatch):
    # MSFT: record 08-20, paid 09-10 (credited 09-11), all 50 shares sold 08-30; the 09-11 sync opens its window 09-02
    def rows(cfg, api, params, key):
        if api == kis.HOLDINGS_AT:  # nothing held today; 50 on the base date
            return [] if params["BASS_DT"] == "20260911" else [{"pdno": "MSFT", "cblc_qty13": "50", "bass_exrt": "1400"}]
        if api == kis.TRADES:  # the sale, found only when the trade search reaches back to 08-30
            return [{"pdno": "MSFT", "ovrs_excg_cd": "NASD"}] if params["ERLM_STRT_DT"] <= "20260830" else []
        if api == kis.RIGHTS:
            return [{"pdno": "MSFT", "acpl_bass_dt": "20260820", "bass_dt": "20260821", "alct_frcr_unpr": "0.91"}]
        return [{"ca_title": "현금배당", "record_dt": "20260820", "pay_dt": "20260910"}]
    monkeypatch.setattr(kis, "_rows", rows)
    [div] = kis._dividends(CFG, "kis:x", date(2026, 9, 2), date(2026, 9, 11))
    assert (div["date"], div["raw"]["shares"], div["raw"]["usd"]) == (date(2026, 9, 11), 50.0, 38.67)  # 45.50 gross less 15%


def test_failed_dividends_skip_the_days_cash_legs(monkeypatch):
    # an unsubtracted dividend would read as a deposit in infer.py: no legs that day, the window merges into the next
    monkeypatch.setattr(kis, "_snapshot", lambda cfg: (10_000_000.0, 1_000.0, 0.0))
    monkeypatch.setattr(kis, "_dividends", lambda *a: (_ for _ in ()).throw(RuntimeError("KIS CTRGT011R: HTTP 500")))
    monkeypatch.setattr(kis, "_cash_legs", lambda cfg, today, rp: {"krw": 1_000.0, "usd": 5.0, "rp_usd": 0.0, "rate": 1400.0})
    monkeypatch.setattr(kis, "_settlements", lambda *a: [])
    out = kis.fetch(CFG, date(2026, 9, 25), date(2026, 9, 1))
    assert out["cash_obs"] == [] and out["snapshots"][0]["value"] == 10_000_000.0
    assert out["snapshots"][0]["rate"] == 1400.0  # the legs' rate still prices the snapshot in dollars
    assert out["fx"] == [{"date": "2026-09-25", "currency": "USD", "krw": 1400.0}]  # and the day's flows if Toss fails
    assert out["errors"] == ["dividends (today's cash legs skipped too): RuntimeError: KIS CTRGT011R: HTTP 500"]


def test_credit_day_skips_weekends_and_new_year():
    assert kis._credit_day(date(2026, 1, 1)) == date(2026, 1, 2)
    assert kis._credit_day(date(2026, 2, 7)) == date(2026, 2, 9)
    assert kis._credit_day(date(2026, 2, 5)) == date(2026, 2, 5)


def _ovs(pdno, dvsn, qty, avg, now, cost, value, wcrc, excg="NASD", settled=None):  # CTRP6504R output1 row, 외화 mode
    return {"pdno": pdno, "prdt_name": f"{pdno} ETF", "prdt_dvsn": dvsn, "cblc_qty13": settled or qty,
            "ccld_qty_smtl1": qty, "pchs_rmnd_wcrc_amt": wcrc,
            "avg_unpr3": avg, "ovrs_now_pric1": now, "frcr_pchs_amt": cost, "frcr_evlu_amt2": value,
            "evlu_pfls_amt2": str(float(value) - float(cost)), "bass_exrt": "1360.00000000", "buy_crcy_cd": "USD",
            "ovrs_excg_cd": excg, "tr_mket_name": "나스닥"}


def test_holdings_merge_mini_rows_and_convert_at_bass_exrt(api):
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE)]
    api.routes["TTTC8434R"] = [Resp({"rt_cd": "0", "msg1": "", "output2": [{}], "output1": [  # contract field names
        {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10", "pchs_avg_pric": "65000", "pchs_amt": "650000",
         "prpr": "72000", "evlu_amt": "720000", "evlu_pfls_amt": "70000", "evlu_pfls_rt": "10.77"},
        {"pdno": "000660", "prdt_name": "SK하이닉스", "hldg_qty": "0", "pchs_avg_pric": "0", "pchs_amt": "0",
         "prpr": "200000", "evlu_amt": "0"}]})]  # sold today
    api.routes["CTRP6504R"] = [Resp({"rt_cd": "0", "msg1": "", "output2": [], "output3": {}, "output1": [
        _ovs("JEPQ", "01", "150.00000000", "59.1000", "61.15500", "8865.00000", "9173.250000", "12553727.0",
             settled="147.00000000"),  # live: 3 bought, not settled yet
        _ovs("JEPQ", "03", "0.41258300", "60.1175", "61.15500", "24.80346", "25.231513", "35717.0"),  # 미니스탁
        _ovs("BITO", "01", "0.00000000", "12.40", "11.19000", "0", "0", "0", "AMEX")]})]
    (acct,) = kis.holdings(CFG)
    params = {tr: p for tr, _, p in api.gets}
    assert params["CTRP6504R"]["WCRC_FRCR_DVSN_CD"] == "02" and params["TTTC8434R"]["INQR_DVSN"] == "02"
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE)]
    assert acct["account_id"] == kis.fetch(CFG, date(2026, 9, 25), None)["accounts"][0]["id"]
    assert acct["total_krw"] == 1651869889547.0  # _snapshot(), not a sum of rows
    jepq, sam = acct["positions"]  # biggest first; the zero-quantity rows are gone
    assert (jepq["symbol"], jepq["market"], jepq["currency"], jepq["price"]) == ("JEPQ", "NASD", "USD", 61.155)
    assert jepq["quantity"] == pytest.approx(150.412583)
    assert jepq["value_krw"] == pytest.approx((9173.25 + 25.231513) * 1360)  # live: 12475620 + 34315 in 원화 mode
    # KRW paid for the settled shares (FX included) + the unsettled 3 at today's value
    assert jepq["cost_krw"] == pytest.approx(12553727 + 35717 + 3 * 61.155 * 1360)
    assert jepq["avg_price"] == pytest.approx((8865.0 + 24.80346) / 150.412583)
    assert jepq["pnl_krw"] == pytest.approx(jepq["value_krw"] - jepq["cost_krw"])
    assert jepq["pnl_krw"] < 0 < (9173.25 + 25.231513) - (8865.0 + 24.80346)  # a USD gain, a KRW loss
    assert jepq["pnl_rate"] == pytest.approx(jepq["pnl_krw"] / jepq["cost_krw"])
    assert jepq["pnl_ex_fx"] is False
    # in dollars, FX left out: a gain where the KRW figure shows a loss
    assert jepq["cost_local"] == pytest.approx(8865.0 + 24.80346)
    assert jepq["pnl_local"] == pytest.approx((9173.25 + 25.231513) - jepq["cost_local"])
    assert jepq["pnl_local_rate"] == pytest.approx(jepq["pnl_local"] / jepq["cost_local"])
    assert jepq["pnl_krw"] == pytest.approx(jepq["pnl_local"] * 1360 + (jepq["cost_local"] * 1360 - jepq["cost_krw"]))
    assert sam == {"symbol": "005930", "name": "삼성전자", "market": "KRX", "currency": "KRW", "quantity": 10.0,
                   "price": 72000.0, "avg_price": 65000.0, "value_krw": 720000.0, "cost_krw": 650000.0,
                   "pnl_krw": 70000.0, "pnl_rate": pytest.approx(70000 / 650000), "pnl_ex_fx": False,
                   "cost_local": 650000.0, "pnl_local": 70000.0, "pnl_local_rate": pytest.approx(70000 / 650000)}


def test_krw_cost_unsettled_sell_drops_its_average_share():
    row = _ovs("VOO", "01", "4.00000000", "600", "700", "2400", "2800", "3200000.0", settled="5.00000000")
    assert kis._krw_cost(row) == pytest.approx(3200000 * 4 / 5)


def test_holdings_empty_account_is_all_rest(api):
    api.routes["CTRP6548R"] = [Resp(ACCOUNT_BALANCE)]
    api.routes["TTTC8434R"] = [Resp({"rt_cd": "0", "msg1": "", "output1": [], "output2": [{}]})]
    api.routes["CTRP6504R"] = [Resp({"rt_cd": "1", "msg_cd": "KIER2620", "msg1": "조회할 자료가 없습니다"})]
    assert kis.holdings(CFG) == [{"account_id": "kis:12345678-01", "total_krw": 1651869889547.0, "positions": []}]


def test_settlements_land_the_next_kr_business_day_minis_in_krw(monkeypatch):
    trades = [  # sttl_dt is the US date; KIS moves the cash the next KR settlement morning (10-09 한글날, then a weekend)
        {"sttl_dt": "20261008", "sll_buy_dvsn_cd": "02", "loan_dvsn_name": "미니스탁", "wcrc_excc_amt": "20000",
         "tr_frcr_amt2": "13.9", "frcr_excc_amt_1": "13.9", "dmst_wcrc_fee": "49"},
        {"sttl_dt": "20261008", "sll_buy_dvsn_cd": "02", "loan_dvsn_name": "현금", "wcrc_excc_amt": "700000",
         "tr_frcr_amt2": "500.0", "frcr_excc_amt_1": "501.25", "dmst_wcrc_fee": "1750"},  # fee is paid in USD
        {"sttl_dt": "20261009", "sll_buy_dvsn_cd": "01", "loan_dvsn_name": "현금", "wcrc_excc_amt": "140000",
         "tr_frcr_amt2": "100.0", "frcr_excc_amt_1": "99.75", "dmst_wcrc_fee": "350"},
    ]
    monkeypatch.setattr(kis, "_rows", lambda cfg, api, params, key: trades)
    monkeypatch.setattr(kis, "_business_days", lambda cfg, s, e: [date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 13)])
    out = kis._settlements(CFG, "kis:x", date(2026, 10, 1), date(2026, 10, 13))
    assert out == [  # all land on 10-12: the mini in KRW with its KRW fee, the rest in USD net of their fees
        {"account_id": "kis:x", "date": "2026-10-12", "usd": pytest.approx(-501.25 + 99.75), "krw": -20000 - 49},
    ]


def test_snapshot_reads_the_rp_row_only_from_the_verified_layout(api):
    rows = [{"pchs_amt": "0", "evlu_amt": "0"} for _ in range(20)]
    rows[7] = {"pchs_amt": "3851470", "evlu_amt": "3852610"}
    api.routes["CTRP6548R"] = [Resp({**ACCOUNT_BALANCE, "output1": rows}), Resp({**ACCOUNT_BALANCE, "output1": rows[:19]})]
    assert kis._snapshot(CFG)[2] == 3851470.0
    assert kis._snapshot(CFG)[2] is None  # 19 rows: an unknown layout, so no RP leg and no inference


def test_business_days_follow_the_settlement_calendar_and_stop_paging(api):
    page = lambda start, flags: Resp({"rt_cd": "0", "msg1": "ok", "output": [  # noqa: E731
        {"bass_dt": (start + timedelta(days=i)).strftime("%Y%m%d"), "bzdy_yn": "Y", "sttl_day_yn": f}
        for i, f in enumerate(flags)]}, tr_cont="M")  # the calendar never says "last page"
    api.routes["CTCA0903R"] = [page(date(2026, 4, 29), "YYNNNY"), page(date(2026, 5, 5), "NY")]
    days = kis._business_days(CFG, date(2026, 4, 29), date(2026, 5, 7))
    assert days == [date(2026, 4, 29), date(2026, 4, 30), date(2026, 5, 4), date(2026, 5, 6), date(2026, 5, 7)]
    assert [g[0] for g in api.gets].count("CTCA0903R") == 2  # limit = 9 days // 24 + 2 pages; 05-07 is a weekday guess


def test_cash_legs_read_settlement_basis_cash_and_the_rp(api):
    api.routes["CTRP6010R"] = [Resp({"rt_cd": "0", "msg1": "ok", "output1": [{"bass_exrt": "1400.0"}],
                                     "output2": [{"crcy_cd": "USD", "frcr_dncl_amt_2": "12.50", "frst_bltn_exrt": "1400.0"}],
                                     "output3": {"tot_dncl_amt": "1084310"}})]
    assert kis._cash_legs(CFG, date(2026, 9, 25), 3_851_470) == {
        "krw": 1084310.0, "usd": 12.5, "rp_usd": pytest.approx(2751.05), "rate": 1400.0}
