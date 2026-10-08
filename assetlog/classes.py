"""Asset classes for the holdings donut (보유 탭 '자산 종류별'): what each won of the ledger is invested in.

Live positions go by symbol (CLASS, weights summing to 1); a symbol not listed counts as 주식. An account's value
beyond its positions (예수금, 외화, RP) is 현금·예금. Accounts without live positions: the DC pension is its ETF
(DC_SYMBOL) plus the cash of its latest snapshot, principal-only savings (principal.py) are 현금·예금, the gold
account (gold.py) is 금, and any other one (a broker whose live read failed) counts whole as 주식.
Add a symbol to CLASS when a holding is something else.
"""
from . import gold, principal

CLASSES = ("주식", "채권", "금", "코인", "현금·예금")
CLASS = {
    "BITO": {"코인": 1},                    # ProShares Bitcoin Strategy: bitcoin futures
    "438100": {"주식": 0.5, "채권": 0.5},    # ACE 미국나스닥100미국채혼합50액티브: half Nasdaq-100, half US Treasuries
}
STOCK, CASH = {"주식": 1}, {"현금·예금": 1}


def breakdown(accounts, cfg, dc_cash=0.0):
    """[{"class", "value_krw"}] in CLASSES order, from /api/holdings' accounts."""
    out = dict.fromkeys(CLASSES, 0.0)

    def add(weights, value):
        for k, w in weights.items():
            out[k] += value * w

    for a in accounts:
        total = a.get("total_krw") or 0.0
        if a["source"] == "live":
            for p in a["positions"]:
                add(CLASS.get(p["symbol"], STOCK), p["value_krw"])
            add(CASH, total - sum(p["value_krw"] for p in a["positions"]))
        elif a["id"] == cfg.get("DC_ACCOUNT"):
            add(CLASS.get(cfg.get("DC_SYMBOL", ""), STOCK), total - (dc_cash or 0.0))
            add(CASH, dc_cash or 0.0)
        elif a["id"] in principal.accounts(cfg):
            add(CASH, total)
        elif a["id"] == gold.account(cfg):
            add({"금": 1}, total)
        else:
            add(STOCK, total)
    return [{"class": k, "value_krw": v} for k, v in out.items() if abs(v) >= 1]
