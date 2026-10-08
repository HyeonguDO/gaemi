"""Importers. KIS: synthetic files from docs/contracts/imports.json (fixtures/kis_all.csv: the 2-row-header
전체거래내역 CSV layout). Toss: pdftotext lines in the layout of a real 거래내역서 (2026-09), names and numbers synthetic."""
import json
import shutil
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from assetlog import db, importers, infer, sync

FIX = Path(__file__).parent / "fixtures"


def mk():
    conn = db.connect(":memory:")
    db.upsert_account(conn, "a", "kis", "KIS")
    return conn


def ledger(conn):
    return [tuple(r) for r in conn.execute("SELECT date, kind, amount, memo FROM transactions ORDER BY date, id")]


def test_kis_all_csv_two_row_layout():
    conn, data = mk(), (FIX / "kis_all.csv").read_bytes()
    assert importers.import_bytes(conn, "a", "kis_xlsx", data) == {"imported": 7, "unknown": 1, "skipped": 1}
    assert ledger(conn) == [
        ("2024-01-02", "DEPOSIT", 1_000_000, "타사이체입금"),  # 연금납입금전환 marker skipped
        ("2024-01-03", "INTERNAL", -100_015, "Smart+거래소주식매수"),  # ETF name with 배당 stays out of memo
        ("2024-01-31", "DIVIDEND", 846, "ETF분배금입금"),  # 1,000 - 세금 154
        ("2024-02-01", "INTEREST", 361, "예탁금이용료"),  # 411 - 50, as in the real file
        ("2024-02-02", "WITHDRAW", -50_000, "Smart+타사이체출금"),
        ("2024-12-20", "INTERNAL", 54_893, "Smart+거래소주식매도"),
        ("2024-12-31", "UNKNOWN", 100_000, "채권만기상환"),
    ]
    # Same file in cp949 re-imports onto the same keys.
    importers.import_bytes(conn, "a", "kis_xlsx", data.decode().encode("cp949"))
    assert len(ledger(conn)) == 7


def test_kis_overlapping_export_dedupes():
    conn, lines = mk(), (FIX / "kis_all.csv").read_text().splitlines()
    importers.import_bytes(conn, "a", "kis_xlsx", "\n".join(lines[:8]).encode())  # header + first 3 rows
    importers.import_bytes(conn, "a", "kis_xlsx", "\n".join(lines[:2] + lines[4:]).encode())  # rows 2..7
    assert len(ledger(conn)) == 7


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_kis_hts_xlsx_code_subcell_fx_and_tax_check():
    data = xlsx([
        ["한국투자증권 계좌별 거래내역"],
        ["거래일", "거래종류", None, "종목명", "잔고번호", "거래수량", "환율", "거래금액", "수수료", "유가잔고"],
        [None, None, None, None, None, "거래단가", None, "정산금액", "거래세", "세금"],
        [datetime(2025, 3, 14), "0110", "해외증권배당금입금", "APPLE INC", "", 10, 1350.5, 2.5, 0, 10],
        [None, None, None, None, None, 0, None, 2.13, 0, 0.37],
        ["2025.03.15", "0020", "HTS당사이체입금", "", "", 0, 0, 300000, 0, 0],
        [None, None, None, None, None, 0, None, 300000, 0, 0],
        ["2025.03.20", "0110", "배당금입금", "삼성전자", "", 10, 0, 10000, 0, 10],
        [None, None, None, None, None, 0, None, 8000, 0, 1540],  # 10,000 - 1,540 != 8,000
        ["2025.03.21", "0120", "해외주식매수", "APPLE INC", "", 1, 1351, 200, 0.1, 11],
        [None, None, None, None, None, 200, None, 200.1, 0, 0],
    ])
    conn = mk()
    assert importers.import_bytes(conn, "a", "kis_xlsx", data) == {"imported": 4, "unknown": 2, "skipped": 0}
    assert ledger(conn) == [
        # 환율 rows: the currency of 거래금액 is a guess, so income/flows go to review; trades stay INTERNAL
        ("2025-03-14", "UNKNOWN", round((2.5 - 0.37) * 1350.5, 2), "해외증권배당금입금"),
        ("2025-03-15", "DEPOSIT", 300_000, "HTS당사이체입금"),
        ("2025-03-20", "UNKNOWN", 8460, "배당금입금"),
        ("2025-03-21", "INTERNAL", round(-200.1 * 1351, 2), "해외주식매수"),
    ]


def test_kis_cash_tab_html_xls_with_spans():
    html = """<html><head><meta charset="euc-kr"></head><body><table>
    <tr><th>거래일</th><th rowspan=2>거래종류</th><th>입금액</th><th rowspan=2>수수료</th><th>예수금</th><th>상대계좌명</th></tr>
    <tr><th>거래시각</th><th>출금액</th><th>CMA</th><th>상대금융기관</th></tr>
    <tr><td>2025.04.01</td><td rowspan=2>이체입금</td><td>500,000</td><td rowspan=2>0</td><td>500,000</td><td>홍길동</td></tr>
    <tr><td>09:00:00</td><td>0</td><td>0</td><td>카카오뱅크</td></tr>
    <tr><td>2025.04.02</td><td rowspan=2>이체출금</td><td>0</td><td rowspan=2>0</td><td>300,000</td><td>홍길동</td></tr>
    <tr><td>10:00:00</td><td>200,000</td><td>0</td><td>카카오뱅크</td></tr>
    <tr><td colspan=2>합계</td><td>500,000</td><td>0</td><td></td><td></td></tr>
    </table></body></html>"""
    conn = mk()
    assert importers.import_bytes(conn, "a", "kis_xlsx", html.encode("cp949")) == {"imported": 2, "unknown": 0, "skipped": 0}
    assert ledger(conn) == [("2025-04-01", "DEPOSIT", 500_000, "이체입금"), ("2025-04-02", "WITHDRAW", -200_000, "이체출금")]
    raw = conn.execute("SELECT raw FROM transactions WHERE memo = '이체출금'").fetchone()[0]
    assert "카카오뱅크" in raw and "10:00:00" in raw


def test_kis_rejects_biff_and_unknown_layout():
    with pytest.raises(ValueError, match="xlsx"):
        importers.import_bytes(mk(), "a", "kis_xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504))
    with pytest.raises(ValueError, match="거래금액"):
        importers.import_bytes(mk(), "a", "kis_xlsx", "거래일,거래종류,금액\n2025.1.2,이체입금,100\n".encode())
    with pytest.raises(ValueError, match="거래일"):
        importers.import_bytes(mk(), "a", "kis_xlsx", "날짜,내용\n".encode())


def test_kis_dividend_without_tax_column_goes_to_review():
    data = "거래일,거래종류,거래금액,잔액\n2025.1.2,배당금입금,\"1,000\",\"1,000\"\n".encode()
    conn = mk()
    assert importers.import_bytes(conn, "a", "kis_xlsx", data)["unknown"] == 1
    assert ledger(conn) == [("2025-01-02", "UNKNOWN", 1000, "배당금입금")]


def fake_pdf(monkeypatch, text):
    monkeypatch.setattr(importers, "_pdf_text", lambda data: text)


# Lines as pdftotext -layout prints a real 거래내역서 (2026-09), names and numbers synthetic. USD rows: KRW values on the first
# line, the ($ …) values on the next; 환율 is blank on plain KRW rows. Balances chain row to row.
TOSS_REAL = (
    "원화 거래내역                                          수량단위 : 주, 원 | 금액단위 : 원\n"
    "거래일자         거래구분        종목명(종목코드)            환율     거래수량    거래대금             정산금액"
    "           단가   수수료      거래세       제세금   변제/연체합        잔고       잔액\n"
    "2026.01.23   이체입금        홍OO                            0   300,000        300,000            0      0       0         0        0          0 300,004\n"
    "2026.01.23   환전원화출금                       1,465.85      0   299,996           299,996         0      0       0         0        0          0        8\n"
    "2026.03.06   오픈뱅킹입금(한국투자 모으기잔액부족                    0    20,000    20,000    0     0     0     0        0    0 20,008\n"
    "                                                                                     4 / 26\n"
    "2026.04.30   이자입금                                   0        92        82    0     0     0    10        0    0 20,090\n"
    "2026.05.29   외화이자세금출금                               0       20        20     0     0     0    20        0    0 20,070\n"
    "달러 거래내역                                          수량단위 : 주, 달러 | 금액단위 : 원, 달러\n"
    "2026.01.01   구매           프로셰어즈 QQQ 3배 ETF   1,434.90 0.152438      11,479           11,479          75,303                0           0            0       3.105274         218,621\n"
    "  ($ 7.999948)        ($ 8.00)    ($ 52.480011)     ($ 0.00)    ($ 0.00)      ($ 0.00)                        ($ 152.36)\n"
    "  (US74347X8314)\n"
    "2026.01.02   외화증권배당금입금    프로셰어즈 QQQ 3배 ETF   1,434.90 3.105274          315             315                    0           0        57              0               0        218,937\n"
    "  ($ 0.22)        ($ 0.22)         ($ 0.00)    ($ 0.00)    ($ 0.04)      ($ 0.00)                        ($ 152.58)\n"
    "2026.03.23   외화증권배당금입금   아이셰어즈 배당성장주 ETF   1,494.90 8.214537     3,542          3,542                  0          0      627               0            0     231,634\n"
    "  ($ 2.37)      ($ 2.37)        ($ 0.00)   ($ 0.00)   ($ 0.42)      ($ 0.00)                  ($ 154.95)\n"
    "2026.04.23   외화세금환급   프로셰어즈 QQQ 3배 ETF   1,477.60         0            44           44                 0          0          0             0             0    228,998\n"
    "  ($ 0.03)     ($ 0.03)         ($ 0.00)   ($ 0.00)   ($ 0.00)      ($ 0.00)                  ($ 154.98)\n"
    "2026.04.29   판매     프로셰어즈 QQQ 3배 ETF   1,473.30       3    278,917         278,630          92,972         279            14             0     0.105274    506,962\n"
    "  ($ 189.315)     ($ 189.12)       ($ 63.105)   ($ 0.19)   ($ 0.01)      ($ 0.00)                 ($ 344.10)\n"
)


def test_toss_real_layout(monkeypatch):
    fake_pdf(monkeypatch, TOSS_REAL)
    conn = mk()
    assert importers.import_bytes(conn, "a", "toss_pdf", b"%PDF-") == {"imported": 10, "unknown": 0, "skipped": 0}
    assert ledger(conn) == [
        ("2026-01-01", "INTERNAL", -11_479, "구매 프로셰어즈 QQQ 3배 ETF"),  # 정산금액, KRW line
        ("2026-01-02", "DIVIDEND", 315, "외화증권배당금입금 프로셰어즈 QQQ 3배 ETF"),  # already net of 제세금
        ("2026-01-23", "DEPOSIT", 300_000, "이체입금 홍OO"),
        ("2026-01-23", "INTERNAL", -299_996, "환전원화출금"),
        ("2026-03-06", "DEPOSIT", 20_000, "오픈뱅킹입금(한국투자 모으기잔액부족"),
        ("2026-03-23", "DIVIDEND", 3_542, "외화증권배당금입금 아이셰어즈 배당성장주 ETF"),
        ("2026-04-23", "DIVIDEND", 44, "외화세금환급 프로셰어즈 QQQ 3배 ETF"),
        ("2026-04-29", "INTERNAL", 278_630, "판매 프로셰어즈 QQQ 3배 ETF"),  # after fee and tax
        ("2026-04-30", "INTEREST", 82, "이자입금"),
        ("2026-05-29", "INTEREST", -20, "외화이자세금출금"),
    ]
    importers.import_bytes(conn, "a", "toss_pdf", b"%PDF-")
    assert len(ledger(conn)) == 10  # re-import dedupes
    raw = json.loads(conn.execute("SELECT raw FROM transactions WHERE date = '2026-03-23'").fetchone()[0])
    assert raw["usd"] == 2.37 and raw["usd_line"].startswith("($ 2.37)")  # a number: infer.py subtracts it


def test_toss_balance_mismatch_fails_loudly(monkeypatch):
    fake_pdf(monkeypatch, TOSS_REAL.replace("($ 154.95)", "($ 155.15)"))  # a misread 10 cents
    with pytest.raises(ValueError, match="USD 잔액"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-")


def test_toss_usd_row_without_dollar_line_fails_loudly(monkeypatch):
    fake_pdf(monkeypatch, "달러 거래내역\n"
             "2026.01.02   외화증권배당금입금   X   1,434.90 3.105274   315   315   0   0   57   0   0   218,937\n"
             "  ($ 0.22)   ($ 0.22)   ($ 0.00)   ($ 0.00)   ($ 0.04)   ($ 152.58)\n")  # six values, not seven
    with pytest.raises(ValueError, match="USD row without"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-")


def test_toss_row_outside_section_or_misaligned_fails(monkeypatch):
    fake_pdf(monkeypatch, "2026.01.23   이체입금   홍OO   0   300,000   300,000   0   0   0   0   0   0   300,004\n")
    with pytest.raises(ValueError, match="section"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-")
    fake_pdf(monkeypatch, "원화 거래내역\n2026.01.23   이체입금   홍OO   300,000   300,004\n")
    with pytest.raises(ValueError, match="layout"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-")


def test_toss_missing_pdftotext(monkeypatch):
    monkeypatch.setattr(importers.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="pdftotext"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-1.4")
    with pytest.raises(ValueError, match="not a PDF"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"hello")


def test_csv_in_out_columns_cp949():
    data = "날짜,적요,입금액,출금액,잔액\n2026-01-05,이체입금,\"1,000,000\",0,\"1,000,000\"\n" \
           "2026-01-06,이체출금,0,\"300,000\",\"700,000\"\n합계,,\"1,000,000\",\"300,000\",\n"
    conn = mk()
    assert importers.import_bytes(conn, "a", "csv", data.encode("cp949")) == {"imported": 2, "unknown": 0, "skipped": 1}
    assert ledger(conn) == [("2026-01-05", "DEPOSIT", 1_000_000, "이체입금"), ("2026-01-06", "WITHDRAW", -300_000, "이체출금")]


def test_csv_unreadable_date_fails_loudly():
    for d in ("2026년 1월 5일", "01/05/2026", "26.01.05"):
        with pytest.raises(ValueError, match="date"):
            importers.import_bytes(mk(), "a", "csv", f"날짜,적요,금액\n{d},이체입금,1000000\n".encode())


def test_csv_identical_rows_keep_count_across_overlapping_imports():
    conn = mk()
    two = "﻿date,memo,amount\n2026-01-05,커피,-4500\n2026-01-05,커피,-4500\n"
    assert importers.import_bytes(conn, "a", "csv", two.encode()) == {"imported": 2, "unknown": 2, "skipped": 0}
    importers.import_bytes(conn, "a", "csv", (two + "2026-01-06,이체입금,10000\n").encode())
    assert [r[2] for r in ledger(conn)] == [-4500, -4500, 10000]


def test_csv_needs_memo_and_amount():
    with pytest.raises(ValueError, match="memo"):
        importers.import_bytes(mk(), "a", "csv", "날짜,금액\n2026-01-05,100\n".encode())
    with pytest.raises(ValueError, match="unknown format"):
        importers.import_bytes(mk(), "a", "xls", b"")


def kis_csv(*rows):
    return ("거래일,거래종류,거래금액,세금,잔액\n" + "".join(f"{r}\n" for r in rows)).encode()


def estimates(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT date, kind, amount FROM transactions WHERE source = 'kis_infer' ORDER BY date")]


def estimated(conn):  # cash seen daily 09-01..05: 09-03 a deposit, 09-04 a domestic sale infer can't see, 09-05 out
    for d, krw in (("01", 0), ("02", 0), ("03", 505_000), ("04", 556_000), ("05", 456_000)):
        db.upsert_cash_obs(conn, "a", f"2026-09-{d}", krw, 0.0, 0.0, 1400.0)
    infer.apply(conn, "a", [], "2026-08-01")
    assert estimates(conn) == [("2026-09-03", "DEPOSIT", 505_000), ("2026-09-04", "DEPOSIT", 51_000),
                               ("2026-09-05", "WITHDRAW", -100_000)]


def test_statement_replaces_the_estimates_inside_its_days_even_edited_ones_for_good():
    conn = mk()
    estimated(conn)
    db.set_kind(conn, conn.execute("SELECT id FROM transactions WHERE date = '2026-09-03'").fetchone()[0],
                "DEPOSIT", 500_000)  # confirmed in 검토, which locks it
    db.upsert_transaction(conn, "a", "2026-09-04", 1000, "해외배당 X", "kis_api", "div:X:20260903", "DIVIDEND")
    out = importers.import_bytes(conn, "a", "kis_xlsx", kis_csv(
        "2026.09.02,예탁금이용료,10,0,10", "2026.09.03,타사이체입금,505000,0,505010",
        "2026.09.04,Smart+거래소주식매도,50000,0,555010", "2026.09.04,ETF분배금입금,1000,0,556010"))
    assert out["replaced_estimates"] == 3  # both estimates inside 09-02..09-04 and the computed dividend
    assert out["replaced_edited"] == [{"date": "2026-09-03", "kind": "DEPOSIT", "amount": 500_000,
                                       "memo": "입금 (잔고로 추정)"}]
    infer.apply(conn, "a", [], "2026-08-01")  # the statement's days are not estimated again
    assert estimates(conn) == [("2026-09-05", "WITHDRAW", -100_000)]
    assert [tuple(r) for r in conn.execute("SELECT date, kind, source FROM transactions WHERE kind IN "
                                           "('DEPOSIT', 'DIVIDEND') ORDER BY date")] == [
        ("2026-09-03", "DEPOSIT", "kis_xlsx"), ("2026-09-04", "DIVIDEND", "kis_xlsx")]


def test_partial_csv_replaces_only_the_estimates_its_own_rows_book():
    conn = mk()
    estimated(conn)
    for d in ("02", "04"):
        db.upsert_transaction(conn, "a", f"2026-09-{d}", 1000, "해외배당 X", "kis_api", f"div:X:202609{d}", "DIVIDEND")
    # dividends only: the computed dividend of the same day goes, the flow estimates between stay
    out = importers.import_bytes(conn, "a", "csv", "날짜,금액,적요\n2026-09-02,1000,해외증권배당금입금\n"
                                 "2026-09-05,300,해외증권배당금입금\n".encode())
    assert out["replaced_estimates"] == 1 and len(estimates(conn)) == 3 and db.coverage(conn, "a") == []
    assert [r[0] for r in conn.execute("SELECT date FROM transactions WHERE source = 'kis_api'")] == ["2026-09-04"]
    # a deposit replaces the estimate whose window holds it, not its neighbours
    out = importers.import_bytes(conn, "a", "csv", "날짜,금액,적요\n2026-09-03,505000,타사이체입금\n".encode())
    assert out["replaced_estimates"] == 1
    assert estimates(conn) == [("2026-09-04", "DEPOSIT", 51_000), ("2026-09-05", "WITHDRAW", -100_000)]


def test_statement_keeps_an_estimate_whose_window_opens_before_its_first_day():
    conn = mk()
    db.upsert_cash_obs(conn, "a", "2026-08-28", 0, 0.0, 0.0, 1400.0)
    db.upsert_cash_obs(conn, "a", "2026-09-02", 510_000, 0.0, 0.0, 1400.0)  # PC off: an 08-29 deposit, seen 09-02
    infer.apply(conn, "a", [], "2026-08-01")
    out = importers.import_bytes(conn, "a", "kis_xlsx", kis_csv(  # even with a deposit of its own in that window
        "2026.09.02,타사이체입금,500000,0,500000", "2026.09.08,타사이체입금,1000,0,501000"))
    assert "replaced_estimates" not in out and estimates(conn) == [("2026-09-02", "DEPOSIT", 510_000)]


def test_sync_skips_computed_dividends_a_statement_covers(monkeypatch):
    conn = mk()
    importers.import_bytes(conn, "a", "kis_xlsx", kis_csv("2026.09.02,ETF분배금입금,1000,0,1000",
                                                          "2026.09.04,예탁금이용료,10,0,1010"))
    div = lambda d: {"account_id": "a", "date": d, "amount": 900, "memo": "해외배당 X",  # noqa: E731
                     "source_key": f"div:X:{d}", "raw": {"usd": 0.65}}
    broker = SimpleNamespace(REQUIRED=(), fetch=lambda cfg, today, since: {
        "accounts": [], "snapshots": [], "transactions": [div("2026-09-03"), div("2026-09-10")]})
    monkeypatch.setattr(sync, "BROKERS", {"kis": broker})
    sync.sync(conn, {}, date(2026, 9, 26))
    assert [r[0] for r in conn.execute("SELECT date FROM transactions WHERE source = 'kis_api'")] == ["2026-09-10"]
    # a transcription (csv) booked 09-12's dividend: the computed one for that day stays out, the week's refetch too
    importers.import_bytes(conn, "a", "csv", "날짜,금액,적요\n2026-09-12,900,해외증권배당금입금 X\n".encode())
    broker.fetch = lambda cfg, today, since: {"accounts": [], "snapshots": [], "transactions": [div("2026-09-12")]}
    sync.sync(conn, {}, date(2026, 9, 26))
    assert [r[0] for r in conn.execute("SELECT date FROM transactions WHERE source = 'kis_api'")] == ["2026-09-10"]


def test_statements_imported_before_the_coverage_table_still_cover_their_days(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_account(conn, "t", "toss", "토스")
    for d in ("2026-01-02", "2026-09-25"):
        db.upsert_transaction(conn, "t", d, 100, "이자입금", "toss_pdf", d)
    conn.execute("DROP TABLE coverage")
    conn.commit()
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO coverage VALUES ('t', 'kis_xlsx', '2025-06-01', '2026-01-01')")  # adjacent ranges merge
    assert db.coverage(conn, "t") == [("2025-06-01", "2026-09-25")]


def test_kis_one_row_file_with_a_line_under_the_header_keeps_every_row():
    data = kis_csv("전일잔액,,,,0", "2025.01.02,이체입금,1000,0,1000", "2025.01.03,이체입금,2000,0,3000",
                   "2025.01.05,이체출금,500,0,2500")
    conn = mk()
    assert importers.import_bytes(conn, "a", "kis_xlsx", data)["imported"] == 3
    assert [r[2] for r in ledger(conn)] == [1000, 2000, -500]


def test_kis_memo_never_comes_from_the_종목명_column():
    data = "거래일,거래종류,종목명,거래금액,세금,잔액\n2025.01.02,,TIGER 미국배당다우존스,\"1,000,000\",0,0\n".encode()
    conn = mk()
    importers.import_bytes(conn, "a", "kis_xlsx", data)
    assert ledger(conn) == [("2025-01-02", "UNKNOWN", 1_000_000, "")]


def test_crafted_files_cant_blow_up_the_grid(monkeypatch):
    wb = Workbook()
    wb.active["A1"], wb.active["GR1"] = "거래일", "x"  # a far cell: openpyxl pads every row up to it
    buf = BytesIO()
    wb.save(buf)
    assert max(map(len, importers._grid(buf.getvalue()))) <= importers.MAX_COLS
    html = "<table><tr><th>거래일</th><th colspan=100000 rowspan=100000>x</th></tr><tr><td>1</td></tr></table>"
    assert max(map(len, importers._html_rows(html))) < 2 * importers.MAX_COLS
    monkeypatch.setattr(importers, "MAX_ROWS", 3)
    with pytest.raises(ValueError, match="more than 3 rows"):
        importers._grid("날짜,적요,금액\n2026-01-01,a,1\n2026-01-02,a,1\n2026-01-03,a,1\n".encode())


def test_pdftotext_runs_with_a_timeout(monkeypatch):
    def run(*args, **kw):
        assert kw.get("timeout")
        raise importers.subprocess.TimeoutExpired(args[0], kw["timeout"])
    monkeypatch.setattr(importers.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(importers.subprocess, "run", run)
    with pytest.raises(ValueError, match="pdftotext ran over"):
        importers.import_bytes(mk(), "a", "toss_pdf", b"%PDF-1.4")


def test_implausible_numbers_and_dates_fail_loudly():
    for v in ("inf", "nan", "-Infinity", "1e300"):
        with pytest.raises(ValueError, match="plausible"):
            importers.import_bytes(mk(), "a", "csv", f"날짜,적요,금액\n2026-01-05,이체입금,{v}\n".encode())
    with pytest.raises(ValueError, match="out of range"):  # 환율 × 거래금액
        importers.import_bytes(mk(), "a", "kis_xlsx", "거래일,거래종류,거래금액,환율\n2025.1.2,해외주식매도,1e12,1e12\n".encode())
    for d in ("2062-01-05", "1989-12-31", "0001-01-01"):
        with pytest.raises(ValueError, match="date outside"):
            importers.import_bytes(mk(), "a", "csv", f"날짜,적요,금액\n{d},이체입금,1000\n".encode())
    with pytest.raises(ValueError, match="no dated transaction rows"):
        importers.import_bytes(mk(), "a", "csv", "날짜,적요,금액\n합계,,1000\n".encode())
