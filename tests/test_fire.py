"""The FIRE tab's simulation (the fire:begin..fire:end block of index.html), run in node."""
import json
import re
import shutil
import subprocess

import pytest

from assetlog import web

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
SIM = re.search(r"// fire:begin.*?// fire:end", (web.STATIC / "index.html").read_text(), re.S).group(0)


def js(expr):
    out = subprocess.run(["node", "-e", f"{SIM}\nconsole.log(JSON.stringify({expr}))"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_dividend_tax():
    small, big = js("[fireDivTax(10e6), fireDivTax(100e6)]")
    assert small == {"us": 1_500_000, "kr": 0}  # under 2,000만: the US 15% covers Korea's 14%
    # 2,000만 at 14% + progressive on 7,850만 (100M - 20M - 1.5M 기본공제) = 2.8M + 13.08M; minus the US 15M, plus 10% local
    assert big["us"] == 15_000_000 and big["kr"] == pytest.approx(968_000)


def test_capital_gains_tax():
    assert js("[fireCgt(2.5e6), fireCgt(12.5e6), fireCgt(302.5e6 + 1e6)]") == pytest.approx([0, 2_200_000, 66_275_000])


PLAN = ("fireNorm({goal: 'drawdown', age: 49, fire_age: 50, end_age: 52, monthly: 100000, inflation: 0, pension_age: 60,"
        " items: [{name: 'g', match: 'g', kind: 'gold', unit: 'krw_month', amount: 0, ret: 0}]})")


def start(value):
    return f"fireStart({PLAN}, {{accounts: [{{id: 'g', total_krw: {value}, positions: []}}]}}, 1400)"


def test_money_lasts_exactly_as_long_as_it_should():
    # one tax-free account with no growth: 1.2M a year for ages 50 and 51 needs 2.4M
    assert js(f"fireRun({PLAN}, {start(2_400_001)}, 0).ok") is True
    assert js(f"[fireRun({PLAN}, {start(2_300_000)}, 0).ok, fireRun({PLAN}, {start(2_300_000)}, 0).out]") == [False, 51]


def test_required_extra_is_the_smallest_that_works():
    plan = PLAN.replace("amount: 0", "amount: 50000")  # 50,000 a month for 12 months: 600k of the 2.4M needed
    r = js(f"fireSolve({plan}, {start(0)})")
    assert r["x"] == pytest.approx(150_000, rel=1e-3) and r["w"] == [1]  # 150k more a month: 1.8M over the year
    assert js(f"[fireRun({plan}, {start(0)}, 149_000).ok, fireRun({plan}, {start(0)}, 151_000).ok]") == [False, True]


def test_extra_is_split_by_weights():
    items = ("[{name: 'a', kind: 'us', unit: 'krw_month', amount: 300000}, {name: 'b', kind: 'us', unit: 'krw_month', amount: 100000},"
             " {name: 'p', kind: 'pension', unit: 'krw_month', amount: 400000}]")
    w = lambda goal, a="", b="": js(f"fireWeights(fireNorm({{goal: '{goal}', items: {items.replace('amount: 300000', 'amount: 300000' + a).replace('amount: 100000', 'amount: 100000' + b)}}}), {{items: [], other: {{}}, fx: 1400}})")
    assert w("dividend") == [0.75, 0.25, 0]  # no weights: today's split; the pension pays no dividends, so no share
    assert w("drawdown") == pytest.approx([0.375, 0.125, 0.5])
    assert w("dividend", b=", weight: 0.3") == [0, 1, 0]  # one weight entered: only the entered ones take a share
    assert w("dividend", ", weight: 1", ", weight: 3") == [0.25, 0.75, 0]  # relative: they need not sum to 100%


def test_health_insurance_counts_dividends_over_10m_in_full():
    # age 49.99: no month of growth before 50, so the first year pays 10% of the start value
    plan = ("fireNorm({age: 49.99, fire_age: 50, end_age: 51, monthly: 0, inflation: 0, nhis_property: 0,"
            " items: [{name: 'd', match: 'D', kind: 'us', unit: 'krw_month', amount: 0, ret: 0.1, div: 0.1}]})")
    h = lambda v: f"{{accounts: [{{id: 'a', total_krw: {v}, positions: [{{symbol: 'D', value_krw: {v}, cost_krw: {v}}}]}}]}}"
    under, over = js(f"[fireRun({plan}, fireStart({plan}, {h(90e6)}, 1400), 0).rows[0].nhis,"
                     f" fireRun({plan}, fireStart({plan}, {h(110e6)}, 1400), 0).rows[0].nhis]")
    assert under == 0
    assert over == pytest.approx(11_000_000 * (0.0719 + 0.009448))



def dividend_plan(monthly, ret):
    # one US ETF paying 10% a year; age 49.99: no month of growth before 50
    return (f"fireNorm({{age: 49.99, fire_age: 50, end_age: 53, monthly: {monthly}, inflation: 0, nhis_property: 0,"
            f" items: [{{name: 'd', match: 'D', kind: 'us', unit: 'krw_month', amount: 0, ret: {ret}, div: 0.1}}]}})")


def holding(v):
    return f"{{accounts: [{{id: 'a', total_krw: {v}, positions: [{{symbol: 'D', value_krw: {v}, cost_krw: {v}}}]}}]}}"


def test_dividend_goal_counts_net_dividends_only():
    # 90M at 10%: 9M of dividends, under 1,000만 (no health insurance), minus the US 15%: 7.65M a year
    ok, no = (js(f"fireRun({dividend_plan(m, 0.1)}, fireStart({dividend_plan(m, 0.1)}, {holding(90e6)}, 1400), 0).ok")
              for m in (637_000, 638_000))
    assert ok is True and no is False
    r = js(f"fireSolve({dividend_plan(637_000, 0.1)}, fireStart({dividend_plan(637_000, 0.1)}, {holding(90e6)}, 1400))")
    assert r["x"] == 0 and r["maxMonthly"] == pytest.approx(7_650_000 / 12)
    assert r["now"]["short"] is None  # the price holds (10% total return, all of it paid out): so do the dividends
    # the same holding at 100M pays 10M; the surplus reinvested tips it past 1,000만, and the premium on all of it
    # then takes the next year below a target the first year met
    plan = dividend_plan(700_000, 0.1)
    assert js(f"fireRun({plan}, fireStart({plan}, {holding(100e6)}, 1400), 0).short") == 51


def test_dividend_goal_reports_when_a_shrinking_price_drops_below_target():
    # 5% total return on a 10% payout: the price falls 5% a year, and the dividends with it
    plan = dividend_plan(700_000, 0.05)
    r = js(f"fireRun({plan}, fireStart({plan}, {holding(100e6)}, 1400), 0)")
    assert r["ok"] is True and r["short"] == 51


def test_untrusted_plan_is_normalized():
    p = js("fireNorm({age: 'x', fire_age: 999, items: [{kind: 'constructor', unit: '__proto__', amount: -5, div: ''}]})")
    assert p["age"] is None and p["fire_age"] == 100
    assert p["items"] == [{"name": "", "match": "", "kind": "us", "unit": "krw_month", "amount": 0, "ret": None,
                           "div": None, "weight": None, "price": None}]


def test_currencies_convert_once():
    # one share a week: a dollar-priced holding goes through the exchange rate, a won-priced one does not
    plan = ("fireNorm({age: 40, items: [{name: 'u', match: 'U', kind: 'us', unit: 'shares_week', amount: 1, ret: 0, div: 0},"
            " {name: 'k', match: 'K', kind: 'us', unit: 'shares_week', amount: 1, ret: 0, div: 0},"
            " {name: 'd', match: '', kind: 'us', unit: 'usd_bday', amount: 12, ret: 0, div: 0},"
            " {name: 'w', match: '', kind: 'us', unit: 'krw_day', amount: 12, ret: 0, div: 0}]})")
    h = ("{accounts: [{id: 'a', total_krw: 2e6, positions: [{symbol: 'U', value_krw: 1e6, price: 50, currency: 'USD'},"
         " {symbol: 'K', value_krw: 1e6, price: 10000, currency: 'KRW'}]}]}")
    r = js(f"fireSolve({plan}, fireStart({plan}, {h}, 1400)).perMonth")
    assert r == pytest.approx([52 / 12 * 50 * 1400, 52 / 12 * 10000, 12 * 250 / 12 * 1400, 12 * 365 / 12])


def test_blank_total_return_keeps_the_price():
    # 10% payout and no total return entered: the return is the payout alone, so the price holds
    plan = ("fireNorm({age: 49.99, fire_age: 50, end_age: 53, monthly: 0, inflation: 0,"
            " items: [{name: 'd', match: 'D', kind: 'us', unit: 'krw_month', amount: 0, div: 0.1},"
            " {name: 'g', match: 'G', kind: 'gold', unit: 'krw_month', amount: 0}]})")
    a = js(f"fireAssets({plan}, fireStart({plan}, {holding(50e6)}, 1400)).map(x => [x.ret, x.div])")
    assert a[:2] == [[0.1, 0.1], [0, 0]]  # gold, paying nothing, then holds at 0%


def test_item_no_longer_bought_holds_and_its_dividends_buy_the_plan():
    # B is held, not bought: no contribution, no share of x, and its dividends (85% after the US tax) go to A
    plan = ("fireNorm({age: 49, fire_age: 50, end_age: 51, monthly: 0, inflation: 0, items: ["
            "{name: 'a', match: 'A', kind: 'us', unit: 'krw_month', amount: 100000, ret: 0, div: 0},"
            " {name: 'b', match: 'B', kind: 'us', unit: 'none', amount: 999, ret: 0.12, div: 0.12}]})")
    h = ("{accounts: [{id: 'x', total_krw: 10e6, positions: [{symbol: 'B', value_krw: 10e6, cost_krw: 10e6}]}]}")
    s = f"fireStart({plan}, {h}, 1400)"
    assert js(f"fireWeights({plan}, {s})") == [1, 0]
    a, b = js(f"fireRun({plan}, {s}, 0).path.at(-1).vals")[:2]  # the point after the year to 51
    v50 = js(f"fireRun({plan}, {s}, 0).path.find(x => x.age === 50).vals")[:2]
    assert v50[1] == pytest.approx(10e6) and v50[0] == pytest.approx(1.2e6 + 12 * 10e6 * 0.01 * 0.85)


def test_dividend_goal_adds_the_pension_from_its_age():
    # no dividends; a 10M pension drawn evenly from 50 to 52: 5M a year, 5.5% tax, so 393,750 a month
    plan = lambda m: (f"fireNorm({{age: 49.99, fire_age: 50, end_age: 52, monthly: {m}, inflation: 0, pension_age: 50,"
                      " items: [{name: 'p', match: 'P', kind: 'pension', unit: 'krw_month', amount: 0, ret: 0}]})")
    h = "{accounts: [{id: 'P', total_krw: 10e6, positions: []}]}"
    r = js(f"fireRun({plan(393_000)}, fireStart({plan(393_000)}, {h}, 1400), 0)")
    assert r["rows"][0]["pension"] == pytest.approx(5e6) and r["ok"] is True and r["short"] is None
    assert js(f"fireRun({plan(394_000)}, fireStart({plan(394_000)}, {h}, 1400), 0).ok") is False


def test_keep_requires_every_year_and_a_blank_pension_age_pays_nothing():
    plan = dividend_plan(700_000, 0.05).replace("monthly: 700000,", "monthly: 700000, keep: true,")
    r = js(f"fireRun({plan}, fireStart({plan}, {holding(100e6)}, 1400), 0)")
    assert r["short"] == 51 and r["ok"] is False  # the first year met it; the second did not
    nopen = (f"fireNorm({{goal: 'drawdown', age: 49.99, fire_age: 50, end_age: 52, monthly: 100000, inflation: 0, pension_age: null,"
             " items: [{name: 'p', match: 'P', kind: 'pension', unit: 'krw_month', amount: 0, ret: 0}]})")
    run = js(f"fireRun({nopen}, fireStart({nopen}, {{accounts: [{{id: 'P', total_krw: 10e6, positions: []}}]}}, 1400), 0)")
    assert run["ok"] is False and run["out"] == 50 and run["rows"][0]["pension"] == 0  # 10M sits in the pension, never drawn


def test_headline_is_the_first_year_or_with_keep_the_leanest():
    # the shrinking-price case: the first year pays 8.5M (708,333 a month), the next ones less
    first = dividend_plan(700_000, 0.05)
    keep = first.replace("monthly: 700000,", "monthly: 700000, keep: true,")
    a, b = (js(f"fireHeadline({pl}, fireStart({pl}, {holding(100e6)}, 1400)).value") for pl in (first, keep))
    assert a == pytest.approx(8_500_000 / 12) and b < 700_000
