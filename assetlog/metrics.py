"""Growth, Modified Dietz return and dividend yield from snapshots + cash flows.

Conventions:
- Snapshots are end-of-day values; a flow dated d is already inside the snapshot of d.
- A period is (start, end]. V0 = value at start, V1 = value at end.
- investment gain = V1 - V0 - net external flow; Modified Dietz return = gain / (V0 + sum w_i * F_i).
- An account first seen inside the period enters with its first snapshot as a contribution,
  so starting to track an account never shows up as a gain.
- Nothing invested at start (no account yet, or every one at 0) -> the period is measured from the first
  money in (`partial`, `eff_start`); otherwise that money counts only for its share of the period and
  the return is inflated by roughly 1/share.
- Dividends count by their own date inside (start, end], so chained periods neither overlap nor drop one.
  Flows instead wait for the account's next valuation, because V1 doesn't hold them yet.
- A zero balance that no money has moved into since is exact whatever its date (a closed account),
  so it never warns as stale. `span_start` is the earliest valuation date a period actually uses:
  a stale start reaches back past `start`, and a chained rate must be annualized over that span.
- Transfers between own accounts need no special handling: in the total they cancel out.
- Dividends are not flows: they stay inside the account values, so `return` (배당 포함) already holds them.
  `dividend_return` (dividends over the same average-capital denominator; `dividend_yield` is its older name) is
  their share of it and `return_ex_div` (배당 제외) the rest (price moves, interest, costs), so per period
  return = return_ex_div + dividend_return. Annualized and chained (compound) rates are built from each series
  on its own, so they don't add up that way. The yield of the dividend payers held now is payers.py's.
- Dollar basis (`return_usd`, 환율 무시): the same period() on the ledger converted to USD (in_usd). `fx_effect` =
  return - return_usd is the gap between the two, cross term and flow timing included, not a pure FX P&L. KRW cash
  counts as dollars too, so it still carries FX. See docs/fx-usd-return.md.
"""
from bisect import bisect_right
from datetime import date, timedelta
from math import prod

STALE_DAYS = 31  # month-end manual entry is the normal rhythm; warn only when a month is missing


def load(conn):
    """{account_id: {'name', 'institution', 'kind', 'snaps': [(date, value)], 'flows': [...], 'divs': [...]}}"""
    data = {
        r["id"]: {"name": r["name"], "institution": r["institution"], "kind": r["kind"],
                  "snaps": [], "flows": [], "divs": []}
        for r in conn.execute("SELECT id, name, institution, kind FROM accounts ORDER BY id")
    }
    for r in conn.execute("SELECT account_id, date, value FROM snapshots ORDER BY date"):
        data[r["account_id"]]["snaps"].append((date.fromisoformat(r["date"]), r["value"]))
    for r in conn.execute(
        "SELECT account_id, date, kind, amount FROM transactions "
        "WHERE kind IN ('DEPOSIT', 'WITHDRAW', 'DIVIDEND') ORDER BY date"
    ):
        key = "divs" if r["kind"] == "DIVIDEND" else "flows"
        data[r["account_id"]][key].append((date.fromisoformat(r["date"]), r["amount"]))
    return data


def value_at(snaps, d):
    """Latest (date, value) on or before d, or None."""
    i = bisect_right(snaps, (d, float("inf"))) - 1
    return snaps[i] if i >= 0 else None


def in_usd(conn, data, krw_only=()):
    """load()'s data in USD: a snapshot at the rate it was priced at (snapshots.rate), else, like flows and dividends,
    at the fx rate of its date (the latest on or before it). Flows never take an account's own rate, so a same-day
    transfer between own accounts still cancels. Anything before the first fx row is left out, so an account enters
    at its first convertible snapshot (a contribution, see period()). A `krw_only` account (won savings with no FX
    exposure, principal.py) goes at one fixed rate: in dollars it neither gains nor loses when the won moves."""
    fx = [(date.fromisoformat(d), r) for d, r in conn.execute(
        "SELECT date, krw FROM fx WHERE currency = 'USD' ORDER BY date")]
    own = {(a, date.fromisoformat(d)): r for a, d, r in conn.execute(
        "SELECT account_id, date, rate FROM snapshots WHERE rate IS NOT NULL")}

    def usd(xs, aid=None):
        return [(d, v / (own.get((aid, d)) or x[1])) for d, v in xs if (x := value_at(fx, d))]

    def fixed(xs):
        return [(d, v / fx[0][1]) for d, v in xs] if fx else []
    return {aid: {**a, "snaps": fixed(a["snaps"]), "flows": fixed(a["flows"]), "divs": fixed(a["divs"])}
            if aid in krw_only else
            {**a, "snaps": usd(a["snaps"], aid), "flows": usd(a["flows"]), "divs": usd(a["divs"])}
            for aid, a in data.items()}


def with_usd(p, u):
    """p (a KRW period) with u's (the same period of in_usd's data) return as return_usd (+ _annualized), and
    fx_effect = return - return_usd; all None when u measures another span (rates start later) or either return is
    None."""
    if p:
        ok = u and None not in (u["return"], p["return"]) and (
            (u["eff_start"], u["span_start"]) == (p["eff_start"], p["span_start"]))
        p["return_usd"], p["return_usd_annualized"], p["fx_effect"] = (
            (u["return"], u["return_annualized"], p["return"] - u["return"]) if ok else (None, None, None))
    return p


def period(data, start, end, ids=None):
    days = (end - start).days
    if days <= 0:
        return None
    v0 = v1 = net = weighted = divs = 0.0
    warnings, money, fresh, span = [], [], False, start  # money: dates capital enters, clamped to start

    def w(d):  # share of the period the money was invested, clamped for stale starts
        return min(1.0, max(0.0, (end - d).days / days))

    for aid, a in data.items():
        if (ids and aid not in ids) or not a["snaps"] or a["snaps"][0][0] > end:
            continue
        first_d, first_v = a["snaps"][0]
        last_d, last_v = value_at(a["snaps"], end)
        v1 += last_v
        fresh |= last_d > start
        pending = any(last_d < d <= end for d, _ in a["flows"])
        if (last_v or pending) and (end - last_d).days > STALE_DAYS:
            warnings.append(f"{a['name']}: 종료 평가액이 {last_d} 기준")
        if first_d <= start:
            begin, v = value_at(a["snaps"], start)
            v0 += v
            if v:
                money.append(start)
            if v or any(begin < d < start for d, _ in a["flows"]):
                span = min(span, begin)
                if (start - begin).days > STALE_DAYS:
                    warnings.append(f"{a['name']}: 시작 평가액이 {begin} 기준")
        else:
            begin = first_d
            net += first_v
            weighted += first_v * w(first_d)
            if first_v:
                money.append(first_d)
        # Flows after the last valuation belong to the next period (they aren't in V1 yet).
        for d, amt in a["flows"]:
            if begin < d <= last_d:
                net += amt
                weighted += amt * w(d)
                money.append(max(d, start))
        divs += sum(amt for d, amt in a["divs"] if max(begin, start) < d <= end)

    if not fresh:  # no valuation inside the period: unknown, not 0%
        return None
    t0 = min(money, default=start)
    if t0 > start:  # each level starts later, so this ends
        p = period(data, t0, end, ids)
        return p and {**p, "start": start.isoformat(), "partial": True}
    denom = v0 + weighted
    gain = v1 - v0 - net
    ret, ex_div, div_ret = (gain / denom, (gain - divs) / denom, divs / denom) if denom > 0 else (None,) * 3
    return {
        "start": start.isoformat(), "end": end.isoformat(), "days": days,
        "eff_start": start.isoformat(),  # differs from start only for partial periods
        "span_start": span.isoformat(),
        "v0": v0, "v1": v1, "net_flow": net, "gain": gain,
        "growth": v1 - v0, "growth_rate": (v1 - v0) / v0 if v0 > 0 else None,
        "return": ret, "return_annualized": _annualized(ret, days),
        "return_ex_div": ex_div, "return_ex_div_annualized": _annualized(ex_div, days),
        "dividends": divs, "dividend_return": div_ret,
        "dividend_yield": div_ret,  # dividend_return's older name, which the page still reads
        "partial": False,
        "warnings": warnings,
    }


def _annualized(r, days):
    return (1 + r) ** (365 / days) - 1 if r is not None and r > -1 and 30 <= days < 365 else None


def years(data, today):
    firsts = [a["snaps"][0][0] for a in data.values() if a["snaps"]]
    if not firsts:
        return []
    out = []
    for y in range(min(firsts).year, today.year + 1):
        p = period(data, date(y - 1, 12, 31), min(date(y, 12, 31), today))
        if p:
            out.append({"year": y, **p})
    return out


def compound(yearly):
    """Chain-link yearly returns into a cumulative return and, over >= 1 year, an annual rate; the _ex_div pair
    chains return_ex_div the same way."""
    rs = [y for y in yearly if y["return"] is not None]  # return_ex_div shares its denominator: None together
    if not rs:
        return None
    days = (date.fromisoformat(rs[-1]["end"]) - date.fromisoformat(rs[0]["span_start"])).days
    out = {}
    for suffix, key in (("", "return"), ("_ex_div", "return_ex_div")):
        growth = prod(1 + y[key] for y in rs)
        out["cumulative" + suffix] = growth - 1
        out["annual" + suffix] = growth ** (365 / days) - 1 if growth > 0 and days >= 365 else None
    return out


def dividends_by_month(data, ids=None):
    out = {}
    for aid, a in data.items():
        if ids and aid not in ids:
            continue
        for d, amt in a["divs"]:
            key = d.strftime("%Y-%m")
            out[key] = out.get(key, 0.0) + amt
    return dict(sorted(out.items()))


def series(data, today):
    """Month-end points: total value and principal (first value + later net flows)."""
    tracked = [a for a in data.values() if a["snaps"]]
    if not tracked:
        return []
    d0 = min(a["snaps"][0][0] for a in tracked)
    points, m = [], date(d0.year, d0.month, 1)
    while m <= today:
        month_end = (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        points.append(min(month_end, today))
        m = month_end + timedelta(days=1)
    out = []
    for d in points:
        value = principal = 0.0
        for a in tracked:
            first_d, first_v = a["snaps"][0]
            if first_d > d:
                continue
            last_d, last_v = value_at(a["snaps"], d)
            value += last_v
            principal += first_v + sum(amt for fd, amt in a["flows"] if first_d < fd <= last_d)
        out.append({"date": d.isoformat(), "value": value, "principal": principal})
    return out


def year_ago(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # Feb 29
        return d.replace(year=d.year - 1, day=28)


def summary(conn, today, krw_only=()):
    data = load(conn)
    du = in_usd(conn, data, krw_only)
    by = {y["year"]: y for y in years(du, today)}
    yearly = [with_usd(y, by.get(y["year"])) for y in years(data, today)]
    since = compound(yearly)
    if since:  # chained like the KRW figure, and only when every year it chains has a dollar return
        rs = [y for y in yearly if y["return"] is not None]
        c = compound([by[y["year"]] for y in rs]) if all(y["return_usd"] is not None for y in rs) else None
        since.update(cumulative_usd=c and c["cumulative"], annual_usd=c and c["annual"])
    y0, ago = date(today.year - 1, 12, 31), year_ago(today)
    ytd = with_usd(period(data, y0, today), period(du, y0, today))
    ttm = with_usd(period(data, ago, today), period(du, ago, today))
    accounts = []
    for aid, a in data.items():
        last = a["snaps"][-1] if a["snaps"] else None
        accounts.append({
            "id": aid, "name": a["name"], "institution": a["institution"], "kind": a["kind"],
            "value": last[1] if last else None, "as_of": last[0].isoformat() if last else None,
            "ytd": with_usd(period(data, y0, today, ids={aid}), period(du, y0, today, ids={aid})),
        })
    unknown = conn.execute("SELECT COUNT(*) FROM transactions WHERE kind = 'UNKNOWN'").fetchone()[0]
    fx = {r["currency"]: {"krw": r["krw"], "date": r["date"]} for r in conn.execute(
        "SELECT currency, krw, MAX(date) AS date FROM fx GROUP BY currency")}  # sqlite: bare columns follow MAX
    return {
        "fx": fx,
        "usd_from": conn.execute("SELECT MIN(date) FROM fx WHERE currency = 'USD'").fetchone()[0],  # dollar figures start here
        "as_of": today.isoformat(),
        "total_value": sum(a["value"] or 0 for a in accounts),
        "ytd": ytd, "ttm": ttm, "years": yearly, "since_start": since,
        "accounts": accounts, "series": series(data, today),
        "dividends_by_month": dividends_by_month(data), "unknown_count": unknown,
    }
