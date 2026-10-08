import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from assetlog import config, db, sync
from assetlog.brokers import kb

CFG = {"KB_APP_KEY": "K" * 36, "KB_APP_SECRET": "S" * 32, "KB_IP_ADDR": "192.168.0.2", "KB_MAC_ADDR": "1A-2B-3C-4D-5E-67"}
TOKEN = "eyJ" + "t" * 437


def env(body, flag="A", code="0011", msg=""):
    return {"dataHeader": {"resultCode": "200", "resultMessage": "성공", "processFlag": flag, "processCode": code,
                           "processMessage": msg, "processTime": "20260622175350789"}, "dataBody": body}


# Portal samples (docs/contracts/kb.json, test-file/SWQA2301 and SSQM2952), verbatim.
LEDGER_ROW = {
    "fcrncy_tfnd_ra": "000000000000.00", "tfnd_blnc": "000000000001000000", "tx": "0000000000000000",
    "etc_lndng_amt_rpay_amt": "000000000000000000", "dl_sq": "0000000001", "fncng_amt": "000000000000000",
    "fee": "000000000000000000", "is_nm": " " * 50, "fcrncy_amt": "0000000000000.00", "crncy_clsf_nm": " " * 40,
    "fcrncy_tfnd": "000000000000000000", "dl_uprc": " " * 16, "dl_amt": "                     1,000,000",
    "itst_rpay_amt": "000000000000000000", "dl_dt": "20260622", "rcvbl_rpay_amt": "000000000000000000",
    "dl_typ_cd": "01", "arrs_fee": "000000000000", "dprt_cd_nm": "BaaS 플랫폼01", "smry_typ_cd": "001",
    "dl_mkt": " " * 10, "ec_amt": "000000001000000", "dl_txtn_uprc": "00000.000", "ocp_pymt_amt": "000000000000000",
    "rsdnt_tx": "000000000000000", "scrts_blnc_q": " " * 30, "stnd_is_cd": " " * 12,
    "stmt_pr_pymt_amt": "0000000000000000", "dl_tx": " " * 30, "exch_r": "000000.0000", "scrts_rpay_q": " " * 30,
    "trsf_tx": "0000000000000", "cprty_ac_nm": " " * 20, "q": " " * 30, "bnf_blnc_val": "000000000000000",
    "ffs_tx": "000000000000000", "abrd_fee": "0000000000000.00", "smry_nm": "입금",
    "stmt_pr_pymt_amt_rpay": "0000000000000000", "incm_tx": "000000000000000", "etc_chck_amt": "000000000000000",
    "bnf_no": " " * 16, "itst_use_fe": "000000000000"}
LEDGER = {"o_msg": "정상적으로 조회되었습니다.", "nxt_key": " " * 100, "grid_cnt1": "0002", "o_lngth": "01770", "o_clsf": "SC",
          "Record1": [LEDGER_ROW, {**LEDGER_ROW, "tfnd_blnc": "000000000101000000", "dl_sq": "0000000002",
                                   "dl_amt": "                   100,000,000", "ec_amt": "000000100000000"}]}
SSQM2952 = {
    "fncng_amt_sum": "000000000000000000", "fncng_amt": "000000000000000000", "etc_lndng_amt": "000000000000000000",
    "mrtg_shrtg_amt": "000000000000000000", "dy_tfnd": "000000000001000000", "cma_pldg_est_amt": "000000000000000",
    "grid_cnt1": "0001", "nxt2_dy_tfnd": "000000000000639950", "gnl_ln_amt": "000000000000000000",
    "val_amt_sum": "000000000000426500", "brwg_mrtg_csh_est_amt": "000000000000000000",
    "val_pl_sum": "000000000000066450", "tl_data_cnt": "0001", "val_yld": "000000000018.45",
    "fcrncy_tfnd_krw_exch_amt": "000000000000.00", "o_lngth": "01015", "scrts_nt_val_amt": "000000000000426500",
    "o_clsf": "SC", "Record1": [{
        "fncng_amt": "000000000000000000", "is_nm": "삼성전자", "addt_amt": "000000000000000000",
        "byng_amt": "000000000000360050", "ordr_psbl_q_p6": "000000000.000000", "clsf": "현금",
        "val_yld": "000000018.45", "is_dtl_typ_cd": "11", "fee_r_p9": "00.000150000",
        "byng_avr_prc": "0000000360050.00", "ec_q_p6": "000000000.000000", "nstmt_b_q_p6": "000000000.000000",
        "nstmt_s_q_p6": "000000000.000000", "now_prc": "0000000426500.00", "val_amt": "000000000000426500",
        "is_cd": "A005930", "nstmt_s_q": "000000000000000", "fnl_ccls_excg_cd": "KRX", "crncy_cd": "   ",
        "dl_tx_r_p4": "0000.0005", "nxtd_lstng_ccd": "1", "ffs_tx_r_p4": "0000.0015", "nstmt_b_q": "000000000000001",
        "hld_q_p6": "00000000.000000", "ordr_psbl_q": "000000000000001", "hld_q": "000000000000000",
        "ec_q": "000000000000001", "val_pl": "000000000000066450"}],
    "nxt2_dy_o_amt_psbl_amt": "000000000000639950", "itst_npymt_amt": "000000000000000000",
    "val_yld_sum": "000000000018.45", "elw_excs_mgn": "000000000000000000", "byng_amt_sum": "000000000000360050",
    "mrtg_rt": "0000000000", "ndy_o_amt_psbl_amt": "000000000001000000", "nt_byng_amt": "000000000000360050",
    "nrfnd_fncng_amt": "000000000000000000", "o_msg": "정상적으로 조회되었습니다.", "brwg_nd_mrtg_amt": "000000000000000000",
    "nt_asts_val_amt": "000000000001066450", "ndy_tfnd": "000000000001000000", "val_pl": "000000000000066450"}
# The portal's SPQM2226 sample header; its grids are garbled (a TSLA holding shifted into Record1), so the rows
# are rebuilt with the spec's field names: 2 TSLA @ 398.255 USD, 100 USD cash, 1516.37 KRW/USD.
SPQM2226 = {
    "krw_o_amt_psbl_amt": "000000000000000", "etc_lndng_amt": "000000000000000", "nxt_key": " " * 100,
    "grid_cnt2": "0001", "chck_amt": "0000000000000", "tl_asts_exch_val_amt": "000000000078890",
    "krw_tfnd": "000000000000000", "tl_scrts_exch_val_amt": "000000001207805", "tl_krw_val_amt": "000000000001207805",
    "mrtg_ln_amt": "000000000000000", "o_msg": "조회 완료, 더 이상 조회할 자료가 없습니다.",
    "tl_tfnd_exch_val_amt": "000000000000000", "tl_krw_val_pl_amt": "-00000000000000561",
    "asts_val_amt": "000000000000000000", "krw_mgn": "000000000000000", "o_lngth": "01663", "o_clsf": "SC",
    "Record1": [{"crncy_clsf_nm": "USD       ", "tfnd": "0000000000100.00", "std_exch_r": "1516.3700",
                 "tfnd_val_amt": "000000000151637", "o_amt_psbl_amt_p2": "0000000000100.00"},
                {"crncy_clsf_nm": "          ", "tfnd": "0000000000000.00", "std_exch_r": "0000.0000"}],
    "Record2": [{"mkt_clsf_nm": "미국나스닥", "crncy_clsf_nm": "USD", "is_cd": "TSLA        ", "is_nm": "테슬라",
                 "frgn_hld_q_p6": "00000002.000000", "now_prc_p4": "0000000398.2550", "std_exch_r": "1516.3700",
                 "byng_avr_prc_p4": "0000000394.4500", "krw_val_amt": "000000001207805"}]}
US = 2 * 398.255 * 1516.37 + 100 * 1516.37


def row(sq, memo, amount, balance, **kw):
    return {**LEDGER_ROW, "dl_sq": f"{sq:010d}", "smry_nm": memo, "dl_amt": f"{amount:>30,}",
            "ec_amt": f"{amount:015d}", "tfnd_blnc": f"{balance:018d}", **kw}


class Resp:
    def __init__(self, env, status=200):
        self.env, self.status_code, self.text = env, status, json.dumps(env, ensure_ascii=False)

    def json(self):
        return self.env


@pytest.fixture
def api(monkeypatch, tmp_path):
    """routes: TR (lowercase) -> fn(dataBody) -> envelope; calls: (TR or 'token', headers, request json)."""
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)
    no_data = env({"o_msg": "조회할 자료가 없습니다.", "nxt_key": " " * 100, "Record1": []}, "B", "1861")
    fake = SimpleNamespace(calls=[], tokens=[TOKEN], routes={
        "ssqm2952": lambda b: env(SSQM2952), "spqm2226": lambda b: env(SPQM2226, code="0223"),
        "swqa2301": lambda b: env(LEDGER) if b["strt_dt"] <= "20260622" <= b["end_dt"] else no_data})

    def post(url, json, headers, timeout):
        tr = url.rsplit("/", 1)[1]
        fake.calls.append((tr, headers, json))
        if tr == "token":
            assert json["dataBody"] == {"appKey": CFG["KB_APP_KEY"], "appSecret": CFG["KB_APP_SECRET"],
                                        "grantType": "client_credentials"}
            return Resp(env({"access_token": fake.tokens.pop(0), "token_type": "Bearer", "expires_in": 86400}, code="0000"))
        return Resp(fake.routes[tr](json["dataBody"]))

    monkeypatch.setattr(kb.requests, "post", post)
    return fake


def test_full_sync_from_portal_samples(api, tmp_path):
    conn = db.connect(":memory:")
    assert sync.sync(conn, CFG, date(2026, 9, 25))["kb"] == "ok: 1 snapshots, 2 transactions"
    assert [tuple(r) for r in conn.execute("SELECT id, institution, name FROM accounts")] == [("kb:main", "kb", "KB증권")]
    (value, cash), = conn.execute("SELECT value, cash FROM snapshots WHERE date = '2026-09-25'")
    assert cash == 639950 and value == pytest.approx(426500 + 639950 + US)
    assert [tuple(r) for r in conn.execute("SELECT date, kind, amount, memo, source_key FROM transactions ORDER BY id")] == [
        ("2026-06-22", "DEPOSIT", 1_000_000, "입금", "20260622-0000000001"),        # history starts from a 0 balance
        ("2026-06-22", "DEPOSIT", 100_000_000, "입금", "20260622-0000000002")]

    trs = [c for c in api.calls if c[0] != "token"]
    assert len(api.calls) - len(trs) == 1 and json.loads((tmp_path / "tokens.json").read_text())["kb"]["token"] == TOKEN
    assert all(h["Authorization"] == f"bearer {TOKEN}" and h["appKey"] == CFG["KB_APP_KEY"] for _, h, _ in trs)
    assert all(j["dataHeader"] == {"ipAddr": "192.168.0.2", "macAddr": "1A-2B-3C-4D-5E-67"} for _, _, j in api.calls)
    ledger = [j["dataBody"] for tr, _, j in trs if tr == "swqa2301"]
    assert all(len(b) == 19 and b["inq_clsf"] == "1" and b["srt_clsf"] == "1" for b in ledger)
    spans = [(date.fromisoformat(f"{b['strt_dt'][:4]}-{b['strt_dt'][4:6]}-{b['strt_dt'][6:]}"),
              date.fromisoformat(f"{b['end_dt'][:4]}-{b['end_dt'][4:6]}-{b['end_dt'][6:]}")) for b in ledger]
    assert spans[0][0] == kb.HISTORY_START and spans[-1][1] == date(2026, 9, 25)
    assert all(b - a < timedelta(days=kb.WINDOW) for a, b in spans)
    assert all(nxt[0] == prev[1] + timedelta(days=1) for prev, nxt in zip(spans, spans[1:]))


def test_signed_amount_is_the_running_balance_move():
    rows = [row(1, "입금", 1_000_000, 5_000_000),       # incremental sync: only seeds the balance
            row(2, "주식매수", 360_050, 4_639_950),
            row(3, "이체출금", 1_000_000, 3_639_950),
            row(4, "배당금입금", 8_460, 3_648_410, incm_tx="000000000001400", rsdnt_tx="000000000000140"),
            row(5, "해외배당금입금", 0, 3_648_410, fcrncy_amt="0000000000012.34", exch_r="001380.0000"),
            row(6, "예탁금이용료", 120, 3_648_530)]
    out = kb._transactions(rows[::-1], "kb:main", seeded=True)
    assert [(t["source_key"], t["amount"]) for t in out] == [
        ("20260622-0000000002", -360_050), ("20260622-0000000003", -1_000_000), ("20260622-0000000004", 8_460),
        ("20260622-0000000005", 0), ("20260622-0000000006", 120)]
    assert [t.get("kind") or db.classify(t["memo"], t["amount"]) for t in out] == [
        "INTERNAL", "WITHDRAW", "DIVIDEND", "UNKNOWN", "INTEREST"]  # a foreign-cash-only dividend waits for review
    assert out[0]["raw"] is rows[1]


def test_balance_that_disagrees_with_ec_amt_is_loud():
    rows = [row(1, "입금", 1_000_000, 5_000_000), row(2, "입금", 1_000_000, 7_000_000)]
    with pytest.raises(RuntimeError, match=r"20260622-0000000002 '입금'.*ec_amt=1000000"):
        kb._transactions(rows, "kb:main", seeded=False)
    seed = row(1, "입금", 1_000_000, 5_000_000)
    for bad in (row(2, "해외배당금입금", 0, 0, fcrncy_amt="0000000000012.34"),  # FX row with no KRW balance
                row(2, "주식입고", 0, 0),                                      # ec_amt 0 does not excuse a jump
                row(2, "주식입고", 0, 0, tfnd_blnc=" " * 18)):                 # blank is not 0
        with pytest.raises(RuntimeError, match="20260622-0000000002"):
            kb._transactions([seed, bad, row(3, "주식매수", 360_050, 4_639_950)], "kb:main", seeded=True)


def test_first_row_that_misfits_the_zero_opening_balance_is_kept_for_review():
    # cash held before HISTORY_START: the dividend's sign is unknown, so it waits in the review queue
    out = kb._transactions([row(1, "배당금입금", 8_460, 6_008_460), row(2, "주식매수", 8_460, 6_000_000)],
                           "kb:main", seeded=False)
    assert [(t["memo"], t["amount"], t.get("kind")) for t in out] == [("배당금입금", 0, "UNKNOWN"), ("주식매수", -8_460, None)]
    assert kb._transactions([row(1, "주식매수", 1, 5)], "kb:main", seeded=False)[0]["kind"] == "INTERNAL"


def test_token_reissued_once_and_nxt_key_followed(api, tmp_path):
    expired = env({}, "B", "T043", "접근 토큰 기간이 만료되었습니다.")
    api.tokens = ["OLD", "NEW", "NEWER"]
    a = kb._Api(CFG)
    replies = [expired, env({"nxt_key": "K1".ljust(100), "Record1": [row(1, "입금", 1, 1)]}),
               env({"nxt_key": " " * 100, "Record1": [row(2, "입금", 1, 2)]})]
    api.routes["swqa2301"] = lambda b: replies.pop(0)
    bodies = a.pages("SWQA2301", kb._ledger_body(date(2026, 6, 22), date(2026, 6, 22)))
    assert [r["dl_sq"] for r in kb._rows(bodies)] == ["0000000001", "0000000002"]
    sent = [(h["Authorization"], j["dataBody"]["nxt_key"]) for tr, h, j in api.calls if tr == "swqa2301"]
    assert sent == [("bearer OLD", ""), ("bearer NEW", ""), ("bearer NEW", "K1".ljust(100))]
    assert json.loads((tmp_path / "tokens.json").read_text())["kb"]["token"] == "NEW"

    api.routes["swqa2301"] = lambda b: expired  # still rejected after the one reissue
    with pytest.raises(RuntimeError, match="T043"):
        a.pages("SWQA2301", kb._ledger_body(date(2026, 6, 22), date(2026, 6, 22)))
    api.routes["swqa2301"] = lambda b: env({"nxt_key": "K1".ljust(100), "Record1": []})
    with pytest.raises(RuntimeError, match="repeated"):
        a.pages("SWQA2301", kb._ledger_body(date(2026, 6, 22), date(2026, 6, 22)))
    keys = iter(range(10**6))
    api.routes["swqa2301"] = lambda b: env({"nxt_key": f"K{next(keys)}".ljust(100), "Record1": []})
    with pytest.raises(RuntimeError, match=f"after {kb.MAX_PAGES} pages"):
        a.pages("SWQA2301", kb._ledger_body(date(2026, 6, 22), date(2026, 6, 22)))


def test_snapshot_composition_is_checked():
    assert kb._domestic(SSQM2952) == (426500 + 639950, 639950)
    usd_row = {**SSQM2952["Record1"][0], "crncy_cd": "USD", "val_amt": "000000000001207805"}
    both = {**SSQM2952, "Record1": SSQM2952["Record1"] + [usd_row], "nt_asts_val_amt": f"{1066450 + 1207805:018d}"}
    assert kb._domestic(both) == (1066450, 639950)  # foreign rows inside the header come from SPQM2226 instead
    with pytest.raises(RuntimeError, match="nt_asts_val_amt=2066450"):
        kb._domestic({**SSQM2952, "nt_asts_val_amt": "000000000002066450"})

    assert float(kb._overseas([SPQM2226])) == pytest.approx(US)
    with pytest.raises(RuntimeError, match="tl_scrts_exch_val_amt"):  # the header has holdings no row explains
        kb._overseas([{**SPQM2226, "Record2": []}])
    with pytest.raises(RuntimeError, match="std_exch_r"):
        kb._overseas([{**SPQM2226, "Record1": [{**SPQM2226["Record1"][0], "std_exch_r": "0000.0000"}]}])


def test_probe_returns_raw_envelopes_masked(api):
    api.routes["spqm2226"] = lambda b: env({}, "B", "I446", "API 사용 권한이 없습니다.")
    api.routes["swqa2301"] = lambda b: env({"nxt_key": " " * 100, "Record1": [row(1, "이체입금", 5, 5, cprty_ac_nm="홍길동")],
                                            "echo": f"bearer {TOKEN} {CFG['KB_APP_SECRET']}"})
    out = kb.probe(CFG)
    assert out["SSQM2952"]["dataBody"]["nt_asts_val_amt"] == "000000000001066450"
    assert out["SPQM2226"]["dataHeader"]["processCode"] == "I446"
    assert out["SWQA2301"]["dataBody"]["Record1"][0]["cprty_ac_nm"] == "***"
    assert out["SWQA2301"]["dataBody"]["echo"] == "bearer *** ***"


def test_rejected_ledger_keeps_the_days_valuation(monkeypatch):
    class FakeApi:
        def __init__(self, cfg): pass
        def pages(self, tr, body): return [{}]
    monkeypatch.setattr(kb, "_Api", FakeApi)
    monkeypatch.setattr(kb, "_domestic", lambda page: (1_000_000, 200_000))
    monkeypatch.setattr(kb, "_overseas", lambda pages: 0)
    monkeypatch.setattr(kb, "_ledger", lambda api, since, today: [])
    def boom(*a, **k): raise RuntimeError("row 3: balance moved 5 but ec_amt 7")
    monkeypatch.setattr(kb, "_transactions", boom)
    out = kb.fetch({"KB_APP_KEY": "k", "KB_APP_SECRET": "s", "KB_ACCOUNT_NO": ""}, date(2026, 9, 25), None)
    assert out["accounts"][0]["id"] == "kb:main"  # blank template value falls back
    assert out["snapshots"][0]["value"] == 1_000_000 and out["transactions"] == []
    assert "ec_amt 7" in out["errors"][0]
