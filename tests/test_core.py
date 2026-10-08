import json
from datetime import date

import pytest

from assetlog import config, db, metrics


def mk():
    conn = db.connect(":memory:")
    db.upsert_account(conn, "a", "kb", "KB")
    db.upsert_account(conn, "b", "toss", "Toss")
    return conn


def run(conn, start, end, ids=None):
    return metrics.period(metrics.load(conn), date.fromisoformat(start), date.fromisoformat(end), ids)


def test_modified_dietz_worked_example():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100_000_000)
    db.upsert_transaction(conn, "a", "2026-07-01", 20_000_000, "이체입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-12-31", 130_000_000)
    p = run(conn, "2025-12-31", "2026-12-31")
    assert p["net_flow"] == 20_000_000 and p["gain"] == 10_000_000
    assert p["return"] == pytest.approx(10_000_000 / (100_000_000 + 20_000_000 * 183 / 365))
    assert p["growth"] == 30_000_000 and p["growth_rate"] == pytest.approx(0.30)


def test_new_account_is_contribution_not_gain():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100_000_000)
    db.upsert_snapshot(conn, "a", "2026-12-31", 100_000_000)
    db.upsert_snapshot(conn, "b", "2026-03-01", 50_000_000)
    db.upsert_snapshot(conn, "b", "2026-12-31", 55_000_000)
    p = run(conn, "2025-12-31", "2026-12-31")
    assert (p["v0"], p["v1"], p["net_flow"], p["gain"]) == (100_000_000, 155_000_000, 50_000_000, 5_000_000)


def test_transfer_between_own_accounts_cancels_in_total():
    conn = mk()
    for acc, v0, v1 in (("a", 100_000_000, 90_000_000), ("b", 50_000_000, 60_000_000)):
        db.upsert_snapshot(conn, acc, "2025-12-31", v0)
        db.upsert_snapshot(conn, acc, "2026-12-31", v1)
    db.upsert_transaction(conn, "a", "2026-05-01", -10_000_000, "이체출금", "manual", "1")
    db.upsert_transaction(conn, "b", "2026-05-02", 10_000_000, "이체입금", "manual", "2")
    total = run(conn, "2025-12-31", "2026-12-31")
    assert total["net_flow"] == 0 and total["gain"] == 0 and total["return"] == 0
    assert run(conn, "2025-12-31", "2026-12-31", {"a"})["net_flow"] == -10_000_000


def test_dividend_yield_shares_return_denominator():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100_000_000)
    db.upsert_transaction(conn, "a", "2026-06-30", 3_000_000, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-12-31", 103_000_000)
    p = run(conn, "2025-12-31", "2026-12-31")
    assert p["return"] == pytest.approx(0.03) and p["dividend_yield"] == pytest.approx(0.03)


def test_flow_after_last_valuation_waits_for_next_period():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100)
    db.upsert_snapshot(conn, "a", "2026-09-24", 100)
    db.upsert_transaction(conn, "a", "2026-09-25", 10, "이체입금", "manual", "1")
    p = run(conn, "2025-12-31", "2026-09-25")
    assert p["net_flow"] == 0 and p["gain"] == 0


def test_years_and_compound():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2024-12-31", 100)
    db.upsert_snapshot(conn, "a", "2025-12-31", 110)
    db.upsert_snapshot(conn, "a", "2026-09-25", 121)
    yearly = metrics.years(metrics.load(conn), date(2026, 9, 25))
    assert [y["year"] for y in yearly] == [2025, 2026]  # 2024 has no span: tracking starts on its last day
    assert yearly[0]["return"] == pytest.approx(0.10) and yearly[1]["return"] == pytest.approx(0.10)
    c = metrics.compound(yearly)
    assert c["cumulative"] == pytest.approx(0.21)
    assert c["annual"] == pytest.approx(1.21 ** (365 / 633) - 1)


def test_partial_period_measures_from_first_snapshot():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2026-12-01", 100)
    db.upsert_snapshot(conn, "a", "2026-12-31", 102)
    p = run(conn, "2025-12-31", "2026-12-31")
    assert p["partial"] and p["eff_start"] == p["span_start"] == "2026-12-01" and p["start"] == "2025-12-31"
    assert p["return"] == pytest.approx(0.02)  # not 0.02 / (30/365)


def test_loss_beyond_minus_100pct_has_no_complex_annualization():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 10)
    db.upsert_transaction(conn, "a", "2026-06-29", 100, "이체입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-06-30", 0)
    p = run(conn, "2025-12-31", "2026-06-30")
    assert p["return"] < -1 and p["return_annualized"] is None
    json.dumps(p)


def test_series_principal_tracks_flows():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2026-01-15", 100)
    db.upsert_transaction(conn, "a", "2026-02-10", 20, "이체입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-02-28", 125)
    s = metrics.series(metrics.load(conn), date(2026, 2, 28))
    assert s == [
        {"date": "2026-01-31", "value": 100, "principal": 100},
        {"date": "2026-02-28", "value": 125, "principal": 120},
    ]


@pytest.mark.parametrize("memo, amount, kind", [
    ("배당금입금", 1000, "DIVIDEND"),
    ("해외주식배당세", -150, "DIVIDEND"),
    ("이체입금", 1000, "DEPOSIT"),
    ("이체출금", -1000, "WITHDRAW"),
    ("입금", -1000, "UNKNOWN"),
    ("예탁금이용료", 12, "INTEREST"),
    ("환전정산입금", 1000, "INTERNAL"),
    ("주식매수", -1000, "INTERNAL"),
    ("타사대체입고", 0, "UNKNOWN"),
    ("알수없음", 5, "UNKNOWN"),
])
def test_classify(memo, amount, kind):
    assert db.classify(memo, amount) == kind


def test_user_decision_survives_reimport():
    conn = mk()
    db.upsert_transaction(conn, "a", "2026-03-02", 0, "타사대체입고", "kb_api", "x1")
    (tx_id, kind), = conn.execute("SELECT id, kind FROM transactions").fetchall()
    assert kind == "UNKNOWN"
    db.set_kind(conn, tx_id, "DEPOSIT", 5_000_000)
    db.upsert_transaction(conn, "a", "2026-03-02", 0, "타사대체입고", "kb_api", "x1")
    assert tuple(conn.execute("SELECT kind, amount FROM transactions").fetchone()) == ("DEPOSIT", 5_000_000)


def test_ttm_dividends_stay_inside_window_despite_stale_start():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2024-12-31", 100_000_000)  # latest value before the TTM start is old
    db.upsert_snapshot(conn, "a", "2026-09-25", 110_000_000)
    for m in range(1, 22):  # 2025-01 .. 2026-09, 100k on the 15th
        y, mo = 2025 + (m - 1) // 12, (m - 1) % 12 + 1
        db.upsert_transaction(conn, "a", f"{y}-{mo:02d}-15", 100_000, "배당금입금", "manual", str(m))
    p = run(conn, "2025-09-25", "2026-09-25")
    assert p["dividends"] == 1_200_000 and p["warnings"]


def test_year_without_any_valuation_is_unknown_not_zero():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2023-12-31", 100)
    db.upsert_snapshot(conn, "a", "2025-12-31", 121)
    yearly = metrics.years(metrics.load(conn), date(2025, 12, 31))
    assert [y["year"] for y in yearly] == [2025]  # 2024 has no snapshot of its own
    assert yearly[0]["return"] == pytest.approx(0.21) and yearly[0]["warnings"]


@pytest.mark.parametrize("memo, amount, kind", [
    ("공모주입고", 0, "INTERNAL"),
    ("무상증자입고", 0, "INTERNAL"),
    ("배당세출금", 150, "UNKNOWN"),
    ("배당세출금", -150, "DIVIDEND"),
    ("배당소득세", 150, "UNKNOWN"),
])
def test_classify_review_findings(memo, amount, kind):
    assert db.classify(memo, amount) == kind


def test_summary_exposes_latest_usd_rate():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2026-09-24", 100)
    db.upsert_fx(conn, "2026-09-20", "USD", 1350, "toss_api")
    db.upsert_fx(conn, "2026-09-24", "USD", 1360, "toss_api")
    assert metrics.summary(conn, date(2026, 9, 25))["fx"] == {"USD": {"krw": 1360, "date": "2026-09-24"}}


def test_zero_at_start_measures_from_first_money():
    conn = mk()  # a: closed long ago at 0; b: opened mid-year
    db.upsert_snapshot(conn, "a", "2025-01-02", 50_000_000)
    db.upsert_snapshot(conn, "a", "2025-03-31", 0)
    db.upsert_snapshot(conn, "b", "2026-06-01", 100_000_000)
    db.upsert_transaction(conn, "b", "2026-08-01", 1_000_000, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "b", "2026-09-25", 105_000_000)
    p = run(conn, "2025-12-31", "2026-09-25")
    assert p["partial"] and p["eff_start"] == "2026-06-01" and p["warnings"] == []
    assert p["return"] == pytest.approx(0.05) and p["dividend_yield"] == pytest.approx(0.01)  # not 0.1155 / 0.0231
    conn = mk()  # README step 4 with 0 typed for an account opened this year
    db.upsert_snapshot(conn, "a", "2025-12-31", 0)
    db.upsert_transaction(conn, "a", "2026-06-01", 100_000_000, "이체입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-09-25", 105_000_000)
    p = run(conn, "2025-12-31", "2026-09-25")
    assert p["return"] == pytest.approx(0.05) and p["eff_start"] == "2026-06-01" and p["warnings"] == []


def test_dividend_after_last_valuation_stays_in_its_year():
    conn = mk()
    for d, v in (("2024-12-31", 100), ("2025-12-23", 110), ("2026-01-02", 112)):  # 12-31 sync missed
        db.upsert_snapshot(conn, "a", d, v)
    db.upsert_transaction(conn, "a", "2025-12-29", 2, "배당금입금", "manual", "1")
    yearly = metrics.years(metrics.load(conn), date(2026, 1, 2))
    assert [(y["year"], y["dividends"]) for y in yearly] == [(2025, 2), (2026, 0)]


def test_compound_annualizes_over_the_stale_span():
    conn = mk()
    for d, v in (("2024-12-31", 100), ("2026-12-31", 121), ("2027-03-01", 121)):  # no valuation in 2025
        db.upsert_snapshot(conn, "a", d, v)
    yearly = metrics.years(metrics.load(conn), date(2027, 3, 1))
    assert yearly[0]["year"] == 2026 and yearly[0]["span_start"] == "2024-12-31"
    assert metrics.compound(yearly)["annual"] == pytest.approx(1.21 ** (365 / 790) - 1)  # not over 425 days


def test_closed_account_at_zero_is_not_stale():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100)
    db.upsert_snapshot(conn, "a", "2026-09-25", 110)
    db.upsert_snapshot(conn, "b", "2025-01-02", 50)
    db.upsert_snapshot(conn, "b", "2025-03-31", 0)
    p = run(conn, "2025-12-31", "2026-09-25")
    assert p["warnings"] == [] and p["span_start"] == "2025-12-31"
    db.upsert_transaction(conn, "b", "2026-05-01", 30, "이체입금", "manual", "1")  # reopened, no valuation yet
    assert run(conn, "2025-12-31", "2026-09-25")["warnings"] == ["Toss: 종료 평가액이 2025-03-31 기준"]


def test_ttm_is_one_calendar_year_across_feb_29():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2027-01-01", 100)
    db.upsert_transaction(conn, "a", "2027-03-02", 1, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2028-02-29", 101)
    assert metrics.summary(conn, date(2028, 3, 1))["ttm"]["dividends"] == 1  # window was 2027-03-02 exclusive
    assert metrics.summary(conn, date(2028, 2, 29))["ttm"]["start"] == "2027-02-28"


def test_return_splits_into_price_only_and_dividends():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2025-12-31", 100_000_000)
    db.upsert_transaction(conn, "a", "2026-06-30", 3_000_000, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-12-31", 108_000_000)
    p = run(conn, "2025-12-31", "2026-12-31")
    assert (p["return"], p["return_ex_div"]) == (pytest.approx(0.08), pytest.approx(0.05))  # 배당 포함 / 제외
    assert p["dividend_return"] == p["dividend_yield"] == pytest.approx(0.03)
    assert p["return"] == pytest.approx(p["return_ex_div"] + p["dividend_return"])
    assert p["return_annualized"] is None and p["return_ex_div_annualized"] is None  # a whole year: nothing to scale


def test_partial_period_splits_and_annualizes_both_returns():
    conn = mk()  # opened 06-01: measured from there, 116 days
    db.upsert_snapshot(conn, "a", "2026-06-01", 100)
    db.upsert_transaction(conn, "a", "2026-07-15", 2, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-09-25", 110)
    p = run(conn, "2025-12-31", "2026-09-25")
    assert p["partial"] and p["days"] == 116
    assert (p["return"], p["return_ex_div"], p["dividend_return"]) == (
        pytest.approx(0.10), pytest.approx(0.08), pytest.approx(0.02))
    assert p["return"] == pytest.approx(p["return_ex_div"] + p["dividend_return"])
    assert p["return_annualized"] == pytest.approx(1.10 ** (365 / 116) - 1)
    assert p["return_ex_div_annualized"] == pytest.approx(1.08 ** (365 / 116) - 1)


def test_compound_chains_the_price_only_return_too():
    conn = mk()
    db.upsert_snapshot(conn, "a", "2024-12-31", 100)
    db.upsert_transaction(conn, "a", "2025-06-30", 5, "배당금입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2025-12-31", 110)
    db.upsert_transaction(conn, "a", "2026-03-31", 2, "배당금입금", "manual", "2")
    db.upsert_snapshot(conn, "a", "2026-09-25", 121)
    s = metrics.summary(conn, date(2026, 9, 25))
    y25, y26 = s["years"]
    assert (y25["return_ex_div"], y26["return_ex_div"]) == (pytest.approx(0.05), pytest.approx(9 / 110))
    c = s["since_start"]
    assert c["cumulative"] == pytest.approx(0.21) and c["cumulative_ex_div"] == pytest.approx(1.05 * (1 + 9 / 110) - 1)
    assert c["annual_ex_div"] == pytest.approx((1.05 * (1 + 9 / 110)) ** (365 / 633) - 1)
    for p in (s["ytd"], s["ttm"], y25, y26):  # every period the page shows has both
        assert p["return"] == pytest.approx(p["return_ex_div"] + p["dividend_return"])
    assert s["ytd"]["return_ex_div_annualized"] == pytest.approx((1 + 9 / 110) ** (365 / 268) - 1)


# Dollar basis (docs/fx-usd-return.md): values at the rate they were priced at, flows and dividends at the fx of
# their date, then the same period().
def usd_ledger(*rates):
    conn = mk()
    for d, r in rates:
        db.upsert_fx(conn, d, "USD", r, "toss_api")
    return conn


def both(conn, start, end, ids=None):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return metrics.with_usd(metrics.period(metrics.load(conn), s, e, ids),
                            metrics.period(metrics.in_usd(conn, metrics.load(conn)), s, e, ids))


@pytest.mark.parametrize("end_usd, gain_usd", [(2000, 0), (2100, 100)])
def test_krw_exchanged_mid_period_books_no_dollar_gain(end_usd, gain_usd):
    # $1,000 at 1000, then ₩1.1M deposited and exchanged at 1100 ($1,000); the rate ends at 1200. Revaluing the dollar
    # part at the start rate instead (the rejected method) would book a spurious 1.1M * (1000/1100 - 1) = -₩100,000.
    conn = usd_ledger(("2025-12-31", 1000), ("2026-07-01", 1100), ("2026-12-31", 1200))
    db.upsert_snapshot(conn, "a", "2025-12-31", 1_000_000)
    db.upsert_transaction(conn, "a", "2026-07-01", 1_100_000, "이체입금", "manual", "1")
    db.upsert_snapshot(conn, "a", "2026-12-31", end_usd * 1200)
    p = both(conn, "2025-12-31", "2026-12-31")
    assert p["return_usd"] == pytest.approx(gain_usd / (1000 + 1000 * 183 / 365), abs=0) and p["return"] > 0
    assert p["fx_effect"] == p["return"] - p["return_usd"]
    assert p["return_usd_annualized"] is None  # a full year: nothing to annualize


def test_snapshot_keeps_the_rate_it_was_priced_at_flows_take_the_days_fx():
    conn = usd_ledger(("2026-09-24", 1360.7))
    db.upsert_snapshot(conn, "a", "2026-09-24", 1_360_000, None, "kis_api", 1360.0)  # KIS bass_exrt, not Toss's
    db.upsert_snapshot(conn, "b", "2026-09-25", 1_360_700)  # no own rate: the latest fx on or before it
    db.upsert_transaction(conn, "a", "2026-09-25", 1_360_700, "이체입금", "manual", "1")
    db.upsert_transaction(conn, "a", "2026-09-25", 13_607, "배당금입금", "manual", "2")
    u = metrics.in_usd(conn, metrics.load(conn))
    d24, d25 = date(2026, 9, 24), date(2026, 9, 25)
    assert u["a"]["snaps"] == [(d24, 1000)]  # 1_360_000 / 1360.7 would be 999.49
    assert u["b"]["snaps"] == u["a"]["flows"] == [(d25, pytest.approx(1000))] and u["a"]["divs"] == [(d25, pytest.approx(10))]


def test_same_day_transfer_cancels_in_dollars_despite_own_rates():
    conn = usd_ledger(("2025-12-31", 1400), ("2026-05-01", 1450), ("2026-12-31", 1361))
    for acc, rate in (("a", 1360.0), ("b", 1360.7)):
        db.upsert_snapshot(conn, acc, "2025-12-31", 1_400_000)
        db.upsert_snapshot(conn, acc, "2026-12-31", 1_500_000, None, "manual", rate)
    db.upsert_transaction(conn, "a", "2026-05-01", -300_000, "이체출금", "manual", "1")
    db.upsert_transaction(conn, "b", "2026-05-01", 300_000, "이체입금", "manual", "2")
    u = metrics.period(metrics.in_usd(conn, metrics.load(conn)), date(2025, 12, 31), date(2026, 12, 31))
    assert u["net_flow"] == 0 and u["v1"] == pytest.approx(1_500_000 / 1360.0 + 1_500_000 / 1360.7)


def test_history_before_the_first_rate_has_no_dollar_return():
    conn = usd_ledger(("2026-03-02", 1450))
    db.upsert_snapshot(conn, "a", "2025-12-31", 1_000_000)
    db.upsert_transaction(conn, "a", "2026-01-15", 500_000, "이체입금", "manual", "1")  # no rate yet: left out
    db.upsert_snapshot(conn, "a", "2026-12-31", 1_600_000)
    db.upsert_snapshot(conn, "b", "2026-06-30", 1_450_000)  # enters after the first rate: partial in both currencies
    db.upsert_snapshot(conn, "b", "2026-12-31", 1_500_000)
    s = metrics.summary(conn, date(2026, 12, 31))
    assert s["usd_from"] == "2026-03-02" and s["ytd"]["return"] is not None
    assert (s["ytd"]["return_usd"], s["ytd"]["fx_effect"], s["years"][-1]["return_usd"]) == (None, None, None)
    assert (s["since_start"]["cumulative_usd"], s["since_start"]["annual_usd"]) == (None, None)
    a, b = s["accounts"]
    assert a["ytd"]["return_usd"] is None
    assert b["ytd"]["partial"] and b["ytd"]["return_usd"] == pytest.approx(1_500_000 / 1450 / 1000 - 1)


def test_constant_rate_gives_the_krw_figures_in_every_period():
    conn = usd_ledger(("2024-12-01", 1300))
    db.upsert_snapshot(conn, "a", "2024-12-31", 100_000_000)
    db.upsert_transaction(conn, "a", "2025-04-10", 20_000_000, "이체입금", "manual", "1")
    db.upsert_transaction(conn, "a", "2025-06-30", 1_000_000, "배당금입금", "manual", "2")
    db.upsert_snapshot(conn, "a", "2025-12-31", 130_000_000)
    db.upsert_snapshot(conn, "b", "2026-02-01", 10_000_000)
    db.upsert_transaction(conn, "b", "2026-05-01", -2_000_000, "이체출금", "manual", "3")
    db.upsert_snapshot(conn, "a", "2026-09-25", 125_000_000, None, "manual", 1300)
    db.upsert_snapshot(conn, "b", "2026-09-25", 9_000_000)
    s = metrics.summary(conn, date(2026, 9, 25))
    for p in (s["ytd"], s["ttm"], *s["years"], *(a["ytd"] for a in s["accounts"])):
        assert p["return_usd"] == pytest.approx(p["return"]) and p["fx_effect"] == pytest.approx(0, abs=1e-12)
        assert p["return_usd_annualized"] == pytest.approx(p["return_annualized"])
    c = s["since_start"]
    assert c["cumulative_usd"] == pytest.approx(c["cumulative"]) and c["annual_usd"] == pytest.approx(c["annual"])


TOSS_ROW = "{}   {}   {} 1   14,505   14,505   14,505   0   0   0   1   100,000"  # 거래내역서 USD row, 환율 first


def test_backfill_fx_on_an_old_ledger_writes_once(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("ALTER TABLE snapshots DROP COLUMN rate")  # the ledger as it was before 2026-09-26
    with conn:
        for aid, inst in (("kis:1", "kis"), ("toss:2", "toss"), ("dc", "hana")):
            db.upsert_account(conn, aid, inst, aid)
            conn.execute("INSERT INTO snapshots VALUES (?, '2025-12-31', 1000000, NULL, 'manual')", (aid,))
        conn.execute("INSERT INTO snapshots VALUES ('kis:1', '2026-09-25', 1100000, 5, 'kis_api')")
        conn.execute("INSERT INTO snapshots VALUES ('toss:2', '2026-09-25', 1100000, 5, 'toss_api')")  # no legs
        db.upsert_cash_obs(conn, "kis:1", "2026-09-25", 5, 1, 0, 1360.0)
        db.upsert_fx(conn, "2026-01-30", "USD", 1428.0, "toss_api")  # a sync's own row on a month-end
        db.upsert_transaction(conn, "kis:1", "2026-01-06", 1442, "해외배당 X", "kis_api", "div:X",
                              "DIVIDEND", json.dumps({"kr_base_dt": "20260105", "rate": 1442.1}))
        for key, day, memo, rate in (("1", "2026-02-02", "구매 테스트 ETF", "1,450.50"),
                                     ("2", "2026-02-03", "환전외화입금", "1,480.00"),  # customer rate: skipped
                                     ("3", "2026-02-04", "구매 테스트 ETF", "")):   # no 환율 column
            db.upsert_transaction(conn, "toss:2", day, -1, memo, "toss_pdf", key, "INTERNAL",
                                  json.dumps({"line": TOSS_ROW.format(day.replace("-", "."), memo, rate), "usd": -10.0}))
    conn.close()
    config.DATA.mkdir(parents=True, exist_ok=True)  # the user's own month-end rates, kept in data/ (KIS bass_exrt)
    (config.DATA / db.MONTH_END_FILE).write_text(json.dumps(
        {"2025-12-31": 1434.9, "2026-01-30": 1427.0, "2026-02-27": 1424.5, "2026-03-31": 1513.4, "2026-04-30": 1476.1,
         "2026-05-29": 1505.8, "2026-06-30": 1541.5, "2026-07-31": 1441.1, "2026-08-31": 1376.5, "2026-09-24": 1360.0}))
    conn = db.connect(path)
    assert [r["rate"] for r in conn.execute("SELECT rate FROM snapshots")] == [None] * 5
    assert db.backfill_fx(conn) == {"fx kis_bass": 9, "fx kis_div": 1, "fx toss_pdf": 1,
                                    "snapshots.rate month-end": 2, "snapshots.rate sync": 1}
    state = [[tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in ("fx", "snapshots")]
    assert db.backfill_fx(conn) == dict.fromkeys(("fx kis_bass", "fx kis_div", "fx toss_pdf", "snapshots.rate month-end",
                                                  "snapshots.rate sync"), 0)
    assert [[tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in ("fx", "snapshots")] == state
    fx = {r["date"]: (r["krw"], r["source"]) for r in conn.execute("SELECT * FROM fx")}
    assert fx["2026-01-30"] == (1428.0, "toss_api") and fx["2025-12-31"] == (1434.9, "kis_bass")
    assert fx["2026-01-05"] == (1442.1, "kis_div") and fx["2026-02-02"] == (1450.5, "toss_pdf")
    assert "2026-02-03" not in fx and "2026-02-04" not in fx
    rates = {(r["account_id"], r["date"]): r["rate"] for r in conn.execute("SELECT * FROM snapshots")}
    assert rates == {("kis:1", "2025-12-31"): 1434.9, ("toss:2", "2025-12-31"): 1434.9, ("dc", "2025-12-31"): None,
                     ("kis:1", "2026-09-25"): 1360.0, ("toss:2", "2026-09-25"): None}
    db.upsert_snapshot(conn, "dc", "2026-09-25", 7, None, "dc_model", 1360)
    db.upsert_snapshot(conn, "kis:1", "2025-12-31", 7)  # a manual overwrite: back to the fx of its date
    assert [tuple(r) for r in conn.execute("SELECT rate FROM snapshots WHERE value = 7 ORDER BY account_id")] == [
        (1360.0,), (None,)]
