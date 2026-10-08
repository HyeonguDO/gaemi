"""payers.annotate with fake per-share sources: no broker is ever called (kis.requests is booby-trapped)."""
import json
from datetime import date, timedelta

import pytest

from assetlog import config, db, dcmodel, payers
from assetlog.brokers import kis

TODAY = date(2026, 9, 26)
CFG = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_CANO": "1", "DC_ACCOUNT": "dc", "DC_SYMBOL": "438100"}
MONTHS = [f"{y}{m:02d}" for y, m in [(2025, 9), (2025, 10), (2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 9)]]
RIGHTS = {  # {record date: USD per share, gross}. VOO drifted: 2025-09-22 is out, 367 days before its latest
    "VOO": {"20250829": 9.0, "20250922": 9.0, "20251222": 1.75, "20260327": 1.75, "20260626": 1.75, "20260924": 1.75},
    "TSLA": {}, "JEPQ": {f"{m}20": 0.4 for m in MONTHS}}
KSD = {"005930": {"2025-09-30": 361.0, "2025-12-31": 361.0, "2026-03-31": 361.0, "2026-06-30": 361.0},  # KRW per share
       "438100": {f"{m[:4]}-{m[4:]}-28": 70.0 for m in MONTHS}}


def pos(symbol, market, currency, qty, price, rate=1.0):
    return {"symbol": symbol, "name": symbol, "market": market, "currency": currency, "quantity": qty, "price": price,
            "value_krw": qty * price * rate}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def no_network(*a, **k):
        raise AssertionError("a test called a broker")
    monkeypatch.setattr(kis.requests, "get", no_network)
    monkeypatch.setattr(kis.requests, "post", no_network)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    calls = {"rights": [], "ksd": [], "closes": 0}

    def rights_of(cfg, pdno, excg, start, end):
        calls["rights"].append((pdno, excg, start, end))
        if pdno == "BOOM":
            raise RuntimeError("KIS CTRGT011R: HTTP 500")
        return [{"pdno": pdno, "acpl_bass_dt": d, "alct_frcr_unpr": str(x)} for d, x in RIGHTS[pdno].items()
                if f"{start:%Y%m%d}" <= d <= f"{end:%Y%m%d}"]  # both ends: the halves' shared day comes back twice

    def distributions(cfg, code, start, end):
        calls["ksd"].append((code, start, end))
        return [(d, x, d) for d, x in KSD[code].items() if start.isoformat() <= d <= end.isoformat()]

    def closes(cfg, code, start, end):
        calls["closes"] += 1
        return {"2026-09-23": 14_800.0, "2026-09-24": 14_900.0, "2026-09-25": 15_000.0}
    monkeypatch.setattr(kis, "rights_of", rights_of)
    monkeypatch.setattr(dcmodel, "distributions", distributions)
    monkeypatch.setattr(dcmodel, "closes", closes)
    conn = db.connect()
    with conn:  # the model's latest day: 540 shares at the 09-24 close + 20,000 cash
        db.upsert_account(conn, "dc", "hana", "퇴직연금", "pension")
        db.upsert_snapshot(conn, "dc", "2026-09-24", 540 * 14_900 + 20_000, 20_000, dcmodel.SOURCE)
    conn.close()
    return calls


def live():
    return {"kis:1": {"positions": [pos("VOO", "AMEX", "USD", 2, 600.0, 1400), pos("TSLA", "NASD", "USD", 1, 400.0, 1400),
                                    pos("005930", "KRX", "KRW", 10, 70_000.0)]},
            "toss:1": {"positions": [pos("JEPQ", "US", "USD", 10, 55.0, 1380), pos("BOOM", "US", "USD", 3, 20.0, 1380)]}}


def test_payers_only_yield_net_of_withholding_with_a_failed_symbol_left_out(fake):
    held = live()
    payers.annotate(CFG, held, {"kis": None, "toss": None, "kb": "API 키가 설정되지 않았어요"}, TODAY, "2026-09-26T10:00:00+09:00")
    p = {x["symbol"]: x for h in held.values() for x in h["positions"]}
    voo = 7 * 0.85 * 2 * 1400                 # per share x (1 - 15% US) x quantity x KRW rate
    samsung = 1444 * (1 - 0.154) * 10         # KR, taxable account: 15.4%
    jepq = 4.8 * 0.85 * 10 * 1380
    dc = 840 * 540                            # pension: untaxed; 540 shares from (value - cash) / the 09-24 close
    assert (p["VOO"]["annual_div_krw"], p["VOO"]["div_yield"]) == (pytest.approx(voo), pytest.approx(7 * 0.85 / 600))
    assert p["005930"]["annual_div_krw"] == pytest.approx(samsung) and p["JEPQ"]["annual_div_krw"] == pytest.approx(jepq)
    assert (p["TSLA"]["annual_div_krw"], p["TSLA"]["div_yield"]) == (0, 0)  # held, pays nothing: not a payer
    assert (p["BOOM"]["annual_div_krw"], p["BOOM"]["div_yield"]) == (None, None)

    block = json.loads((config.DATA / payers.STATE).read_text())
    values = {"VOO": 1_680_000, "005930": 700_000, "JEPQ": 759_000, "438100": 540 * 15_000}  # DC at the latest close
    assert {x["symbol"]: x["value_krw"] for x in block["positions"]} == values  # TSLA and BOOM don't dilute it
    assert [x["symbol"] for x in block["positions"]] == ["438100", "JEPQ", "VOO", "005930"]  # biggest payer first
    assert block["positions"][0] == {"symbol": "438100", "name": "퇴직연금", "account": "dc", "value_krw": 8_100_000,
                                     "annual_div_krw": dc, "yield": pytest.approx(840 / 15_000)}
    annual = voo + samsung + jepq + dc
    assert (block["annual_krw"], block["value_krw"]) == (pytest.approx(annual), sum(values.values()))
    assert block["yield"] == pytest.approx(annual / sum(values.values())) and block["as_of"] == "2026-09-26T10:00:00+09:00"
    assert block["missing"] == [{"symbol": None, "account": "kb", "error": "API 키가 설정되지 않았어요"},
                                {"symbol": "BOOM", "account": "toss:1", "error": "RuntimeError: KIS CTRGT011R: HTTP 500"}]
    start, mid = date(2025, 9, 19), date(2026, 3, 27)  # a year and a week back, in two halves for CTRGT011R
    assert [c for c in fake["rights"] if c[0] == "VOO"] == [("VOO", "AMEX", start, mid), ("VOO", "AMEX", mid, TODAY)]
    assert sorted(fake["ksd"]) == [("005930", start, TODAY), ("438100", start, TODAY)]


def test_per_share_figures_are_fetched_once_a_day(fake):
    payers.annotate(CFG, live(), {}, TODAY, "t1")
    n_rights, n_ksd = len(fake["rights"]), len(fake["ksd"])
    assert n_rights == 7 and n_ksd == 2  # two halves a USD symbol; BOOM fails on its first
    held = live()
    payers.annotate(CFG, held, {}, TODAY, "t2")  # the hourly read: only the failed symbol is asked again
    assert [c[0] for c in fake["rights"][n_rights:]] == ["BOOM"] and len(fake["ksd"]) == n_ksd
    assert held["kis:1"]["positions"][0]["annual_div_krw"] == pytest.approx(7 * 0.85 * 2 * 1400)  # from the cache
    cache = json.loads((config.DATA / payers.CACHE).read_text())
    assert cache["date"] == TODAY.isoformat() and "BOOM" not in cache["per_share"] and cache["per_share"]["TSLA"] == 0
    assert json.loads((config.DATA / payers.STATE).read_text())["as_of"] == "t2"
    payers.annotate(CFG, live(), {}, TODAY + timedelta(days=1), "t3")  # a new day asks again
    assert len(fake["rights"]) == 2 * n_rights + 1 and len(fake["ksd"]) == 2 * n_ksd


def test_nothing_without_the_kis_keys(fake):
    held = live()
    payers.annotate({"TOSS_CLIENT_ID": "x"}, held, {}, TODAY, "t")
    assert "annual_div_krw" not in held["kis:1"]["positions"][0] and not (config.DATA / payers.STATE).exists()
    assert fake == {"rights": [], "ksd": [], "closes": 0}


def test_dc_without_a_model_value_is_reported_missing(fake):
    payers.annotate({**CFG, "DC_ACCOUNT": "nope"}, {}, {}, TODAY, "t")
    block = json.loads((config.DATA / payers.STATE).read_text())
    assert block["positions"] == [] and block["yield"] is None
    assert block["missing"] == [{"symbol": "438100", "account": "nope", "error": "RuntimeError: no DC model value yet: run a sync"}]


def test_a_failed_read_keeps_its_payers_from_the_last_block_and_a_zero_price_needs_none(fake):
    payers.annotate(CFG, live(), {"kis": None, "toss": None}, TODAY, "t1")
    first = json.loads((config.DATA / payers.STATE).read_text())
    held = {"toss:1": {"positions": [pos("JEPQ", "US", "USD", 10, 0.0, 1380), pos("TSLA", "US", "USD", 1, 0.0, 1380),
                                     pos("BOOM", "US", "USD", 3, 20.0, 1380)]}}  # a price of 0: a payer and a non-payer
    payers.annotate(CFG, held, {"kis": "RuntimeError: KIS down", "toss": None}, TODAY, "t2")
    block = json.loads((config.DATA / payers.STATE).read_text())
    assert sorted(block["positions"], key=str) == sorted(first["positions"], key=str)
    assert block["yield"] == first["yield"]
    assert [(m["symbol"], m["account"]) for m in block["missing"]] == [(None, "kis"), ("JEPQ", "toss:1"),
                                                                       ("BOOM", "toss:1")]
    assert (held["toss:1"]["positions"][1]["annual_div_krw"], held["toss:1"]["positions"][1]["div_yield"]) == (0, 0)
