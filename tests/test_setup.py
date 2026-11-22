"""The setup page's server side: saving to .env, never echoing values, and explaining failed checks."""
import os
import stat
from datetime import datetime

import pytest

from assetlog import config, scheduler, setup


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# my notes\nKIS_APP_KEY=old\nDC_ACCOUNT=hana:dc\n")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.setattr(config, "load", lambda *a, **k: dict(
        l.split("=", 1) for l in path.read_text().splitlines() if "=" in l and not l.startswith("#")))
    return path


def test_save_replaces_and_appends_keeping_other_lines(env):
    key = "A" * 36
    assert setup.save({"KIS_APP_KEY": key, "KIS_CANO": " 12345678 ", "TOSS_CLIENT_ID": ""}) == ["KIS_APP_KEY", "KIS_CANO"]
    assert env.read_text() == f"# my notes\nKIS_APP_KEY={key}\nDC_ACCOUNT=hana:dc\nKIS_CANO=12345678\n"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert setup.status()["kis"] == {"KIS_APP_KEY": True, "KIS_APP_SECRET": False, "KIS_CANO": True, "KIS_ACNT_PRDT_CD": False}


def test_save_strips_pasted_header_names_and_refuses_bad_shapes_without_echoing(env):
    setup.save({"CF_ACCESS_CLIENT_ID": "CF-Access-Client-Id: abcdef0123456789.access"})
    assert "CF_ACCESS_CLIENT_ID=abcdef0123456789.access" in env.read_text()
    with pytest.raises(ValueError) as e:
        setup.save({"KIS_CANO": "1234-5678 secretish"})
    assert "계좌번호" in str(e.value) and "secretish" not in str(e.value)
    with pytest.raises(ValueError):
        setup.save({"KIS_APP_KEY": "A" * 36 + "\nTOSS_CLIENT_ID=x"})  # no line injection
    with pytest.raises(ValueError):
        setup.save({"DC_ACCOUNT": "x"})  # only the page's own keys
    assert "TOSS_CLIENT_ID" not in env.read_text()


def test_explain_names_the_cause_and_the_fix():
    why, fix = setup.explain("toss", "TossError: toss POST /oauth2/token: HTTP 403 access_denied: {}", "1.2.3.4")
    assert "1.2.3.4" in why and "허용 IP" in fix
    assert "앱키" in setup.explain("kis", "RuntimeError: KIS tokenP: HTTP 403 EGW00103 유효하지 않은 AppKey입니다.")[0]
    assert "1분" in setup.explain("kis", "RuntimeError: KIS tokenP: HTTP 403 EGW00133 접근토큰 발급 잠시 후 다시 시도하세요")[1]
    assert "연결" in setup.explain("cloud", "ConnectionError: HTTPSConnectionPool(...)")[0]
    assert "PC 키" in setup.explain("cloud", 'RuntimeError: GET /pc/ops: HTTP 403 {"detail":"X-Assetlog-Key missing or wrong"}')[0]


def test_check_reports_missing_values_then_a_broker_failure(env, monkeypatch):
    r = setup.check("toss")
    assert r["ok"] is False and "Client ID" in r["fix"]
    setup.save({"TOSS_CLIENT_ID": "client-0001", "TOSS_CLIENT_SECRET": "s" * 32})
    from assetlog.brokers import toss
    monkeypatch.setattr(toss, "holdings", lambda cfg: (_ for _ in ()).throw(toss.TossError(
        "toss POST /oauth2/token: HTTP 403 access_denied: {}", 403, "access_denied")))
    monkeypatch.setattr(setup, "public_ip", lambda: "5.6.7.8")
    r = setup.check("toss")
    assert r["ok"] is False and "5.6.7.8" in r["why"] and "s" * 32 not in r["error"]
    monkeypatch.setattr(toss, "holdings", lambda cfg: [{"total_krw": 1_000_000}, {"total_krw": 500_000}])
    assert setup.check("toss") == {"ok": True, "summary": "연결됐어요. 계좌 2개, 총자산 1,500,000원"}


def test_scheduler_runs_a_missed_sync_and_the_phone_copy(monkeypatch):
    t = [datetime(2026, 11, 22, 21, 0, tzinfo=scheduler.KST)]
    assert scheduler.due(t[0]).isoformat() == "2026-11-21"
    assert scheduler.due(datetime(2026, 11, 22, 23, 50, tzinfo=scheduler.KST)).isoformat() == "2026-11-22"
    last, calls = ["2026-11-20"], []

    def sync():
        calls.append(("sync", t[0]))
        last[0] = t[0].date().isoformat()
        return len([c for c in calls if c[0] == "sync"]) == 1  # the first run fails transiently, the retry works
    monkeypatch.setattr(scheduler, "_sync", sync)
    monkeypatch.setattr(scheduler, "_cloud", lambda: calls.append(("cloud", t[0])))
    monkeypatch.setattr(scheduler, "last_sync", lambda: datetime.fromisoformat(last[0]).date())
    stop = scheduler.threading.Event()
    steps = iter(range(60))  # 60 ticks of 30 s: half an hour

    def sleep(_):
        t[0] += scheduler.timedelta(seconds=scheduler.TICK)
        if next(steps, None) is None:
            stop.set()
    scheduler.loop(stop, now=lambda: t[0], sleep=sleep)
    syncs = [c[1].strftime("%H:%M") for c in calls if c[0] == "sync"]
    clouds = [c[1].strftime("%H:%M") for c in calls if c[0] == "cloud"]
    assert syncs == ["21:00", "21:10"]  # the missed sync at start, then one retry 10 minutes later
    assert clouds == ["21:00", "21:15", "21:30"]



def test_terminal_wizard_saves_checks_and_explains(env, monkeypatch):
    from assetlog import setup_cli
    answers = iter(["", "12345678", "01",  # KIS: set it up? (default yes), CANO, product code
                    "y", "12345678", "",  # failed: try again, same CANO, keep product code
                    "n",  # Toss: no
                    ""])  # phone copy: no (default)
    secrets = iter(["A" * 36, "B" * 180, "", ""])  # app key, secret; then kept on the retry
    monkeypatch.setattr("builtins.input", lambda q="": next(answers))
    monkeypatch.setattr(setup_cli.getpass, "getpass", lambda q="": next(secrets))
    monkeypatch.setattr(setup, "public_ip", lambda: "5.6.7.8")
    results = iter([{"ok": False, "why": "앱키가 올바르지 않아요", "fix": "다시 복사하세요", "error": "EGW00103"},
                    {"ok": True, "summary": "연결됐어요. 계좌 총자산 1원"}])
    monkeypatch.setattr(setup, "check", lambda svc: next(results))
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    setup_cli.wizard()
    text = "\n".join(out)
    assert "5.6.7.8" in text and "실패: 앱키가 올바르지 않아요" in text and "성공: 연결됐어요" in text
    assert "B" * 180 not in text  # a secret is never printed
    assert f"KIS_APP_SECRET={'B' * 180}" in env.read_text() and "KIS_CANO=12345678" in env.read_text()



def test_a_new_worker_address_needs_new_credentials(env):
    creds = {"CF_ACCESS_CLIENT_ID": "abcdef0123456789.access", "CF_ACCESS_CLIENT_SECRET": "c" * 64, "CLOUD_PC_KEY": "k" * 43}
    setup.save({"CLOUD_URL": "https://gaemi.me.workers.dev", **creds})
    with pytest.raises(ValueError) as e:  # the stored secrets must not follow a swapped address
        setup.save({"CLOUD_URL": "https://evil.example"})
    assert "PC 키" in str(e.value) and "evil.example" not in env.read_text()
    setup.save({"CLOUD_URL": "https://gaemi.me.workers.dev/"})  # the same address: fine
    setup.save({"CLOUD_URL": "https://new.me.workers.dev", **creds})  # a new address with its credentials: fine
    for bad in ("https://169.254.169.254", "https://10.0.0.5:8443", "https://localhost", "https://[::1]"):
        with pytest.raises(ValueError):
            setup.save({"CLOUD_URL": bad, **creds})


def test_cloud_check_asks_past_every_op_id_within_the_workers_limit(env, monkeypatch):
    from assetlog import cloud
    setup.save({"CLOUD_URL": "https://gaemi.me.workers.dev", "CF_ACCESS_CLIENT_ID": "abcdef0123456789.access",
                "CF_ACCESS_CLIENT_SECRET": "c" * 64, "CLOUD_PC_KEY": "k" * 43})
    calls = []
    monkeypatch.setattr(cloud, "_call", lambda s, m, url, **kw: calls.append((url, kw)) or {"mode": "cloud", "pushed_at": None})
    assert setup.check("cloud")["ok"] is True
    after = str(calls[1][1]["params"]["after"])
    assert after.isdigit() and len(after) <= 15  # cloud/src/index.js pull(): /^\d{1,15}$/
