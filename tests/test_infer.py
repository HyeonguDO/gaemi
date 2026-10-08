import json
from datetime import date

import pytest

from assetlog import db, infer

A = "kis:12345678-01"


def obs(d, krw, usd=0.0, rp=0.0, rate=1400.0):
    return {"date": d, "krw": krw, "usd": usd, "rp_usd": rp, "rate": rate}


def test_quiet_days_book_nothing():
    # 10-06: a 미니스탁 buy paid in KRW (fee included); 10-07: the afternoon auto-RP sweeps the USD 예수금
    days = [obs("2026-10-05", 1_000_000, usd=500.0), obs("2026-10-06", 979_950, usd=500.0),
            obs("2026-10-07", 979_950, usd=0.0, rp=500.0)]
    assert infer.windows(days, [{"date": "2026-10-06", "usd": 0.0, "krw": -20_050}], []) == []


def test_plain_deposit_is_exact_to_the_won():
    days = [obs("2026-10-05", 100_000), obs("2026-10-06", 1_100_000 - 20_050)]
    [w] = infer.windows(days, [{"date": "2026-10-06", "usd": 0.0, "krw": -20_050}], [])
    assert (w["amount"], w["kind"], w["exchanged"]) == (1_000_000, "DEPOSIT", False)


def test_deposit_with_auto_exchange_uses_the_combined_figure_snapped():
    # 2,000,000 in; 700,000 of it exchanged at 1,407 (spread) and spent on a $497.51 buy; USD 예수금 left: 0
    days = [obs("2026-10-05", 50_000, rate=1400), obs("2026-10-06", 1_350_000, rate=1400)]
    buy = [{"date": "2026-10-06", "usd": -497.51 + 0.0, "krw": -1_200}]
    # the exchange added 700,000/1,407 = 497.51 USD that the buy consumed: USD side nets to 0 unless we look at KRW
    [w] = infer.windows(days, buy, [])
    assert w["exchanged"] and w["amount"] == 2_000_000 and w["kind"] == "DEPOSIT"
    assert (w["krw_only"], w["combined"], w["usd_residual"]) == (1_301_200, 1_997_714, 497.51)  # kept for review


def test_one_day_usd_timing_noise_cancels_and_keeps_the_krw_figure():
    days = [obs("2026-10-05", 500_000), obs("2026-10-06", 430_000, usd=90.0), obs("2026-10-07", 430_000, usd=0.5)]
    out = infer.windows(days, [], [("2026-10-07", 0.0)])
    assert [(w["date"], w["amount"], w["exchanged"]) for w in out] == [("2026-10-06", -70_000, False)]


def test_dividend_is_not_a_deposit():
    days = [obs("2026-10-05", 0, usd=0.0), obs("2026-10-06", 0, usd=126.35)]
    assert infer.windows(days, [], [("2026-10-06", 126.35)]) == []


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.upsert_account(c, A, "kis", "한투", "brokerage")
    return c


def test_apply_books_after_recorded_flows_and_re_estimates_without_touching_locked_rows(conn):
    db.upsert_transaction(conn, A, "2026-10-01", 300_000, "타사이체입금", "kis_capture", "k1")
    for d, krw in (("2026-09-30", 0), ("2026-10-02", 0), ("2026-10-05", 500_000), ("2026-10-06", 450_000)):
        db.upsert_cash_obs(conn, A, d, krw, 0.0, 0.0, 1400.0)
    assert infer.apply(conn, A, [], "2026-09-01") == (2, [])
    rows = conn.execute("SELECT date, kind, amount, memo, raw FROM transactions WHERE source = 'kis_infer' ORDER BY date").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("2026-10-05", "DEPOSIT", 500_000), ("2026-10-06", "WITHDRAW", -50_000)]
    assert rows[0][3] == "입금 (잔고로 추정)" and json.loads(rows[0][4])["krw_only"] == 500_000
    tx = conn.execute("SELECT id FROM transactions WHERE source = 'kis_infer' AND date = '2026-10-06'").fetchone()[0]
    db.set_kind(conn, tx, "INTERNAL")  # the user says it was not a withdrawal
    db.upsert_cash_obs(conn, A, "2026-10-06", 440_000, 0.0, 0.0, 1400.0)  # re-read: another 10,000 went out
    db.upsert_cash_obs(conn, A, "2026-10-07", 440_000, 0.0, 0.0, 1400.0)
    infer.apply(conn, A, [], "2026-09-01")
    rows = conn.execute("SELECT date, kind, amount FROM transactions WHERE source = 'kis_infer' ORDER BY date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-05", "DEPOSIT", 500_000), ("2026-10-06", "INTERNAL", -50_000)]
    # the window (09-30, 10-02] holds the recorded 10-01 deposit and is never inferred
    assert not conn.execute("SELECT 1 FROM transactions WHERE source = 'kis_infer' AND date <= '2026-10-02'").fetchone()


def test_a_recording_imported_later_replaces_the_estimate(conn):
    for d, krw in (("2026-10-02", 0), ("2026-10-05", 1_000_000), ("2026-10-06", 1_000_000)):
        db.upsert_cash_obs(conn, A, d, krw, 0.0, 0.0, 1400.0)
    assert infer.apply(conn, A, [], "2026-09-01") == (1, [])
    db.upsert_transaction(conn, A, "2026-10-04", 1_000_000, "타사이체입금", "kis_capture", "k2")  # the same deposit, booked
    assert infer.apply(conn, A, [], "2026-09-01") == (1, [])
    assert not conn.execute("SELECT 1 FROM transactions WHERE source = 'kis_infer'").fetchone()


def test_long_sync_gap_is_reported_not_guessed(conn):
    db.upsert_cash_obs(conn, A, "2026-07-31", 0, 0.0, 0.0, 1400.0)
    db.upsert_cash_obs(conn, A, "2026-09-16", 900_000, 0.0, 0.0, 1400.0)  # PC off for 47 days
    n, notes = infer.apply(conn, A, [], "2026-08-07")  # trades only fetched from here
    assert n == 0 and notes and "2026-07-31..2026-09-16" in notes[0]
    assert not conn.execute("SELECT 1 FROM transactions WHERE source = 'kis_infer'").fetchone()


def test_timing_noise_across_a_quiet_weekend_still_cancels():
    days = [obs("2026-10-08", 500_000), obs("2026-10-09", 500_000, usd=90.0),  # Friday: a dividend lands a day early
            obs("2026-10-10", 500_000, usd=90.0), obs("2026-10-11", 500_000, usd=90.0),  # weekend syncs: quiet
            obs("2026-10-12", 500_000, usd=90.5)]  # Monday: the ledger dates the dividend here
    assert infer.windows(days, [], [("2026-10-12", 90.0)]) == []


def test_small_exchange_is_recognised_by_opposite_moves():
    # 07-14: $27.18 bought with 40,350원 (no deposit); 05-15: 1,500,000 in, then $40 sold for 59,912원
    days = [obs("2026-07-13", 100_000, rate=1485.0), obs("2026-07-14", 59_650, usd=27.18, rate=1485.0)]
    assert infer.windows(days, [], []) == []
    days = [obs("2026-05-14", 0, usd=40.0, rate=1499.5), obs("2026-05-15", 1_559_912, usd=0.07, rate=1499.5)]
    [w] = infer.windows(days, [], [])
    assert (w["amount"], w["exchanged"]) == (1_500_000, True)


def test_fills_are_split_by_time_so_a_sync_at_any_hour_works():
    # syncs at 09:00; a 300,000원 buy fills at 15:00 on 05-01: a trade, no deposit or withdrawal anywhere
    days = [dict(obs("2026-04-30", 1_000_000), at="2026-04-30T09:00:00+09:00"),
            dict(obs("2026-05-01", 1_000_000), at="2026-05-01T09:00:00+09:00"),
            dict(obs("2026-05-02", 700_000), at="2026-05-02T09:00:00+09:00")]
    fill = [{"date": "2026-05-01", "at": "2026-05-01T15:00:00+09:00", "usd": 0.0, "krw": -300_000}]
    assert infer.windows(days, fill, [], infer.EXCHANGED["toss_infer"]) == []


def test_apply_uses_only_this_accounts_settlements(conn):
    for d in ("2026-09-01", "2026-09-02"):
        db.upsert_cash_obs(conn, A, d, 1_000_000, 0.0, 0.0, 1400.0)
    other = {"account_id": "kis:other", "date": "2026-09-02", "usd": 0.0, "krw": -300_000}  # sync passes them all
    assert infer.apply(conn, A, [other], "2026-08-01") == (0, [])


def test_a_flow_booked_on_the_day_of_a_morning_sync_covers_the_window_after_that_sync(conn):
    for d, krw in (("2026-09-01", 0), ("2026-09-02", 0), ("2026-09-03", 300_000)):
        db.upsert_cash_obs(conn, A, d, krw, 0.0, 0.0, 1400.0, f"{d}T09:00:00+09:00")
    assert infer.apply(conn, A, [], "2026-08-01", "toss_infer") == (1, [])  # in at 15:00 on 09-02, seen 09-03
    db.upsert_transaction(conn, A, "2026-09-02", 300_000, "이체입금", "toss_pdf", "p1")  # the statement's date
    assert infer.apply(conn, A, [], "2026-08-01", "toss_infer") == (1, [])  # removed, and not booked again
    assert not conn.execute("SELECT 1 FROM transactions WHERE source = 'toss_infer'").fetchone()


def test_after_a_late_night_sync_a_booked_flow_covers_only_its_own_day(conn):
    for d, krw in (("2026-09-01", 0), ("2026-09-02", 100_000), ("2026-09-03", 400_000)):
        db.upsert_cash_obs(conn, A, d, krw, 0.0, 0.0, 1400.0, f"{d}T23:50:00+09:00")
    db.upsert_transaction(conn, A, "2026-09-02", 100_000, "이체입금", "manual", "m1", "DEPOSIT")
    infer.apply(conn, A, [], "2026-08-01", "toss_infer")
    assert [tuple(r) for r in conn.execute("SELECT date, amount FROM transactions WHERE source = 'toss_infer'")] == [
        ("2026-09-03", 300_000)]


def test_a_flow_entered_by_hand_replaces_the_estimate_at_once_but_not_an_edited_one(conn):
    for d, krw in (("2026-09-01", 0), ("2026-09-03", 500_000), ("2026-09-04", 400_000)):
        db.upsert_cash_obs(conn, A, d, krw, 0.0, 0.0, 1400.0)
    infer.apply(conn, A, [], "2026-08-01")  # window 09-02..03: +500,000; 09-04: -100,000
    db.set_kind(conn, conn.execute("SELECT id FROM transactions WHERE date = '2026-09-04'").fetchone()[0], "WITHDRAW")
    assert infer.yield_to_booked(conn, A, date(2026, 9, 2)) == 1
    assert infer.yield_to_booked(conn, A, "2026-09-04") == 0
    assert [r[0] for r in conn.execute("SELECT date FROM transactions WHERE source = 'kis_infer'")] == ["2026-09-04"]


def test_toss_statement_dividends_stored_as_text_are_still_subtracted(conn):
    db.upsert_cash_obs(conn, A, "2026-09-01", 0, 0.0, 0.0, 1380.0)
    db.upsert_cash_obs(conn, A, "2026-09-02", 0, 9.37, 0.0, 1380.0)
    line = "($ 9.37)   ($ 9.37)   ($ 0.00)   ($ 0.00)   ($ 1.65)   ($ 0.00)   ($ 152.44)"  # imported before 2026-09-26
    db.upsert_transaction(conn, A, "2026-09-02", 12_930, "외화증권배당금입금 X", "toss_pdf", "p", "DIVIDEND",
                          json.dumps({"usd": line}))
    assert infer.apply(conn, A, [], "2026-08-01", "toss_infer") == (0, [])  # not a 'deposit' of the dividend
    assert infer._usd(json.dumps({"usd": line}), -12_930) == -9.37  # a tax row takes the row's sign
