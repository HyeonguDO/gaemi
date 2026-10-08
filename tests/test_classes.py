"""classes.breakdown: every won of the holdings lands in exactly one asset class."""
import pytest

from assetlog import classes


def test_breakdown_by_symbol_cash_and_account_mode():
    cfg = {"DC_ACCOUNT": "dc", "DC_SYMBOL": "438100", "ASSETLOG_PRINCIPAL_ONLY": "manual:youth",
           "ASSETLOG_GOLD_ACCOUNT": "manual:gold"}
    accounts = [
        {"id": "kis", "source": "live", "total_krw": 1000,
         "positions": [{"symbol": "VOO", "value_krw": 500}, {"symbol": "BITO", "value_krw": 300}]},  # + 200 cash
        {"id": "dc", "source": "snapshot", "total_krw": 900, "positions": []},  # 100 cash, 800 of the 50/50 ETF
        {"id": "manual:youth", "source": "snapshot", "total_krw": 400, "positions": []},
        {"id": "manual:gold", "source": "snapshot", "total_krw": 250, "positions": []},
        {"id": "toss", "source": "snapshot", "total_krw": 50, "positions": []},  # live read failed: counted as stocks
    ]
    got = {c["class"]: c["value_krw"] for c in classes.breakdown(accounts, cfg, dc_cash=100)}
    assert got == pytest.approx({"주식": 500 + 400 + 50, "채권": 400, "금": 250, "코인": 300, "현금·예금": 200 + 100 + 400})
    assert sum(got.values()) == pytest.approx(sum(a["total_krw"] for a in accounts))
