from datetime import date, timedelta

import pytest

from assetlog import config, db, dcmodel
from assetlog.brokers import kis

# trading days (closes): 10-23 is a Friday, 10-26..28 trading, 10-29 a holiday, then 10-30, 11-02..
P = {"2026-10-22": 15000.0, "2026-10-23": 15000.0, "2026-10-26": 15100.0, "2026-10-27": 15200.0,
     "2026-10-28": 15300.0, "2026-10-30": 15400.0, "2026-11-02": 15500.0, "2026-11-03": 15600.0}


def run(today, **kw):
    args = dict(start="2026-10-22", shares=500, cash=10_000.0, prices=P, today=today, monthly=305_000,
                last_deposit="2026-09-24")
    args.update(kw)
    return dcmodel.replay(**args)


def test_contribution_lands_on_the_last_trading_day_up_to_the_25th_and_buys_whole_shares_later(monkeypatch):
    monkeypatch.setattr(dcmodel, "BUY_LAG", 3)
    events, states = run("2026-11-03")
    assert [(k, d, a) for k, d, a, _ in events] == [
        ("DEPOSIT", "2026-10-23", 305_000),                    # the 25th is a Sunday
        ("INTERNAL", "2026-10-28", -20 * 15300.0)]             # 3rd trading day after: floor(315,000 / 15,300) = 20
    assert states["2026-11-03"] == (520, 315_000 - 20 * 15300.0, 520 * 15600.0 + 315_000 - 20 * 15300.0)
    assert states["2026-10-24"][0] == 500 and states["2026-10-24"][2] == 500 * 15000.0 + 315_000  # weekend: last close


def test_no_contribution_before_its_day_is_known():
    events, _ = run("2026-10-23")  # today is the 23rd: the 24th or 25th could still be a trading day for all we know
    assert events == []


def test_a_booked_deposit_replaces_the_schedule_and_sets_the_new_amount(monkeypatch):
    monkeypatch.setattr(dcmodel, "BUY_LAG", 1)
    events, states = run("2026-11-03", deposits=[("2026-10-23", 400_000)])  # a raise, booked by the user
    assert [(k, d) for k, d, *_ in events] == [("INTERNAL", "2026-10-26")]  # no scheduled deposit in October
    assert events[0][3]["qty"] == 27  # floor(410,000 / 15,100), the first trading day after the deposit


def test_distribution_pays_shares_held_on_the_record_date():
    events, _ = run("2026-11-03", payouts=[("2026-10-26", 288.0, "2026-11-02")], last_deposit=None)
    assert [(k, d, a, x["shares"]) for k, d, a, x in events if k == "DIVIDEND"] == [("DIVIDEND", "2026-11-02", 144_000, 500)]


def test_an_entered_value_re_anchors_to_the_nearest_share_count(monkeypatch):
    monkeypatch.setattr(dcmodel, "BUY_LAG", 3)
    # the model holds 520 shares after 10-28; the real account bought 19 (a pricier fill) and has 519
    value = 519 * 15400.0 + 12_000
    events, states = run("2026-11-03", values={"2026-10-30": value})
    anchor = next(x for k, d, a, x in events if k == "ANCHOR")
    assert anchor == {"shares": 519, "cash": 12_000}
    assert states["2026-11-03"][:2] == (519, 12_000)
    # the same value on a day the contribution is still in cash picks "not yet invested"
    v2 = 500 * 15000.0 + 315_000
    events, _ = run("2026-10-26", values={"2026-10-24": v2})
    assert next(x for k, d, a, x in events if k == "ANCHOR")["shares"] == 500


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    # offline: the KIS holiday calendar answers with plain weekdays, as it does when the call fails
    monkeypatch.setattr(kis, "_business_days", lambda cfg, s, e, flag=None: [
        s + timedelta(days=i) for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5])
    monkeypatch.setattr(dcmodel, "closes", lambda cfg, code, s, e: P)
    monkeypatch.setattr(dcmodel, "distributions", lambda cfg, code, s, e: [])
    c = db.connect(tmp_path / "t.db")
    db.upsert_account(c, "dc", "hana", "퇴직연금", "pension")
    db.upsert_transaction(c, "dc", "2026-09-24", 305_000, "입금", "hana_capture", "k", "DEPOSIT")
    return c


def test_sync_books_model_rows_and_never_overwrites_an_entered_value(conn, monkeypatch):
    monkeypatch.setattr(dcmodel, "closes", lambda cfg, code, s, e: P)
    monkeypatch.setattr(dcmodel, "distributions", lambda cfg, code, s, e: [])
    monkeypatch.setattr(dcmodel, "BUY_LAG", 3)
    db.upsert_snapshot(conn, "dc", "2026-10-27", 7_900_000, None, "manual")
    cfg = {"DC_ACCOUNT": "dc", "DC_START": "2026-10-22,500,10000", "KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_CANO": "1"}
    out = dcmodel.sync(conn, cfg, date(2026, 11, 3))
    assert out.startswith("ok:")
    rows = [tuple(r) for r in conn.execute("SELECT date, kind, source FROM transactions WHERE source = 'dc_model' ORDER BY date")]
    assert rows[0] == ("2026-10-23", "DEPOSIT", "dc_model")
    snaps = dict(conn.execute("SELECT date, source FROM snapshots WHERE account_id = 'dc'").fetchall())
    assert snaps["2026-10-27"] == "manual" and snaps["2026-11-03"] == "dc_model" and "2026-10-24" not in snaps
    dcmodel.sync(conn, cfg, date(2026, 11, 3))  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'dc_model'").fetchone()[0] == len(rows)


def test_contribution_before_a_holiday_counts_from_its_day():
    # 2026-09: the 24th-26th are 추석, so the contribution lands on Wednesday the 23rd
    prices = {"2026-09-21": 15300.0, "2026-09-22": 15250.0, "2026-09-23": 15290.0}
    days = dcmodel.contribution_days(prices, "2026-09-23", calendar=["2026-09-23"])  # the calendar: no business day left
    assert days == {"2026-09-23"}
    assert dcmodel.contribution_days(prices, "2026-09-23") == set()  # no calendar: unknown until the 25th has passed
    events, states = dcmodel.replay("2026-09-22", 612, 11_840.0, prices, "2026-09-23", 305_000, "2026-08-25",
                                    deposit_days=days)
    assert [(k, d, a) for k, d, a, _ in events] == [("DEPOSIT", "2026-09-23", 305_000)]
    assert states["2026-09-23"] == (612, 316_840.0, 612 * 15290.0 + 316_840)  # 9,674,320 that day, the contribution still in cash


def test_contribution_days_match_2025_2026_history():
    from datetime import timedelta
    holidays = {"2025-12-25", "2026-05-25", "2026-09-24", "2026-09-25"}  # the ones that fall on a 25th or the day before
    d, prices = date(2025, 6, 1), {}
    while d <= date(2026, 9, 26):
        if d.weekday() < 5 and d.isoformat() not in holidays:
            prices[d.isoformat()] = 15000.0
        d += timedelta(days=1)
    real = {"2025-06-25", "2025-07-25", "2025-08-25", "2025-09-25", "2025-10-24", "2025-11-25", "2025-12-24", "2026-01-23",
            "2026-02-25", "2026-03-25", "2026-04-24", "2026-05-22", "2026-06-25", "2026-07-24", "2026-08-25", "2026-09-23"}
    assert dcmodel.contribution_days(prices, "2026-09-26") == real  # the 16 monthly contributions in the ledger


def test_buy_countdown_counts_trading_days_before_the_start(monkeypatch):
    monkeypatch.setattr(dcmodel, "BUY_LAG", 3)
    prices = {"2026-10-20": 15000.0, "2026-10-21": 15000.0, "2026-10-22": 15000.0, "2026-10-23": 15100.0,
              "2026-10-26": 15200.0, "2026-10-27": 15300.0}
    # contribution 10-20, state known on 10-22 (not invested yet): the 3rd trading day after 10-20 is 10-23
    events, _ = dcmodel.replay("2026-10-22", 500, 315_000.0, prices, "2026-10-27", 305_000, "2026-10-20", deposit_days=set())
    assert [(k, d) for k, d, *_ in events] == [("INTERNAL", "2026-10-23")]


def test_an_entered_value_with_a_typo_is_not_used_and_is_reported(conn, monkeypatch):
    monkeypatch.setattr(dcmodel, "closes", lambda cfg, code, s, e: P)
    monkeypatch.setattr(dcmodel, "distributions", lambda cfg, code, s, e: [])
    db.upsert_snapshot(conn, "dc", "2026-10-26", 77_000_000, None, "manual")  # an extra digit: 7,700,000 was meant
    cfg = {"DC_ACCOUNT": "dc", "DC_START": "2026-10-22,500,10000", "KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_CANO": "1"}
    out = dcmodel.sync(conn, cfg, date(2026, 11, 3))
    assert "not used (typo?)" in out and "2026-10-26" in out
    value = conn.execute("SELECT value FROM snapshots WHERE account_id = 'dc' AND date = '2026-11-03'").fetchone()[0]
    assert value < 10_000_000  # still the model's ~500-odd shares, not ~5,000


CFG = {"DC_ACCOUNT": "dc", "DC_START": "2026-10-22,500,10000", "KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_CANO": "1"}


def test_a_booked_dividend_replaces_the_models_row(conn, monkeypatch):
    monkeypatch.setattr(dcmodel, "distributions", lambda cfg, code, s, e: [("2026-10-26", 60.0, "2026-11-02")])
    monkeypatch.setattr(dcmodel, "BUY_LAG", 10)  # nothing bought yet: all of it still in cash
    db.upsert_transaction(conn, "dc", "2026-11-03", 29_850, "분배금", "hana_capture", "d1", "DIVIDEND")
    dcmodel.sync(conn, CFG, date(2026, 11, 3))
    divs = conn.execute("SELECT source, amount FROM transactions WHERE kind = 'DIVIDEND'").fetchall()
    assert [tuple(r) for r in divs] == [("hana_capture", 29_850)]  # the bank's line, not also the model's 30,000
    cash = conn.execute("SELECT cash FROM snapshots WHERE account_id = 'dc' AND date = '2026-11-03'").fetchone()[0]
    assert cash == 10_000 + 305_000 + 30_000  # the modelled cash still got the payout


def test_every_sync_rebuilds_the_models_snapshots_and_keeps_entered_ones(conn):
    db.upsert_snapshot(conn, "dc", "2026-10-27", 7_900_000, None, "manual")
    dcmodel.sync(conn, CFG, date(2026, 10, 24))  # a Saturday sync books that day ("today")
    db.upsert_transaction(conn, "dc", "2026-10-23", 400_000, "입금", "manual", "m1", "DEPOSIT")  # the real contribution
    dcmodel.sync(conn, CFG, date(2026, 11, 3))
    snaps = dict(conn.execute("SELECT date, value FROM snapshots WHERE account_id = 'dc' AND date <= '2026-10-24'"))
    assert snaps[max(snaps)] == 500 * 15000 + 10_000 + 400_000  # what 10-24 reads (value_at) is the rebuilt model
    assert tuple(conn.execute("SELECT source, value FROM snapshots WHERE date = '2026-10-27'").fetchone()) == ("manual", 7_900_000)


def test_a_booked_deposit_or_dividend_replaces_the_models_even_an_edited_one(conn):
    db.upsert_transaction(conn, "dc", "2026-10-23", 305_000, "부담금 입금 (예정액으로 추정)", "dc_model", "dep:2026-10", "DEPOSIT")
    tx = conn.execute("SELECT id FROM transactions WHERE source_key = 'dep:2026-10'").fetchone()[0]
    db.set_kind(conn, tx, "DEPOSIT", 360_000)  # the user corrected the amount: still a model row
    db.upsert_transaction(conn, "dc", "2027-01-05", 170_000, "분배금 (추정)", "dc_model", "div:2026-12-30", "DIVIDEND")
    db.upsert_transaction(conn, "dc", "2026-11-23", 1, "x", "dc_model", "dep:2026-11", "DEPOSIT")
    db.set_kind(conn, conn.execute("SELECT id FROM transactions WHERE source_key = 'dep:2026-11'").fetchone()[0], "INTERNAL")
    dcmodel.yield_month(conn, "dc", "2026-10-22")               # the real contribution, booked by hand
    dcmodel.yield_month(conn, "dc", "2027-01-07", "DIVIDEND")   # the real distribution, two days later
    dcmodel.yield_month(conn, "dc", "2026-11-25")               # the user said the November row is no deposit
    left = [r[0] for r in conn.execute("SELECT source_key FROM transactions WHERE source = 'dc_model'")]
    assert left == ["dep:2026-11"]
