"""The setup page's server side (index.html #tab-setup, web.py /api/setup*): which settings are set, saving them to
.env, this server's public IP, and a live check per service that says why it failed and what to do.

Secrets never leave this module: status() reports set/unset only, and check() reports broker messages, which carry
codes and descriptions but never the key (the brokers' error bodies don't echo it).
"""
import ipaddress
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config

# key: (pattern the value must match, max length). Only these keys can be written from the page.
FIELDS = {
    "KIS_APP_KEY": (r"[A-Za-z0-9]{20,64}", 64),
    "KIS_APP_SECRET": (r"[A-Za-z0-9+/=]{100,400}", 400),
    "KIS_CANO": (r"\d{8}", 8),
    "KIS_ACNT_PRDT_CD": (r"\d{2}", 2),
    "TOSS_CLIENT_ID": (r"[\w.-]{8,128}", 128),
    "TOSS_CLIENT_SECRET": (r"[\w.~+/=-]{16,256}", 256),
    "CLOUD_URL": (r"https://[\w.-]+(:\d+)?/?", 200),
    "CF_ACCESS_CLIENT_ID": (r"[\w.-]{16,128}", 128),
    "CF_ACCESS_CLIENT_SECRET": (r"[\w.-]{16,128}", 128),
    "CLOUD_PC_KEY": (r"[\w-]{24,128}", 128),
}
SERVICES = {"kis": ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO", "KIS_ACNT_PRDT_CD"),
            "toss": ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"),
            "cloud": ("CLOUD_URL", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "CLOUD_PC_KEY")}
LABEL = {"KIS_APP_KEY": "앱키", "KIS_APP_SECRET": "앱시크릿", "KIS_CANO": "계좌번호 앞 8자리", "KIS_ACNT_PRDT_CD": "상품코드",
         "TOSS_CLIENT_ID": "Client ID", "TOSS_CLIENT_SECRET": "Client Secret", "CLOUD_URL": "Worker 주소",
         "CF_ACCESS_CLIENT_ID": "서비스 토큰 Client ID", "CF_ACCESS_CLIENT_SECRET": "서비스 토큰 Client Secret",
         "CLOUD_PC_KEY": "PC 키"}
IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")


def status():
    """{service: {key: set?}}: never a value."""
    cfg = config.load()
    return {svc: {k: bool(cfg.get(k)) for k in keys} for svc, keys in SERVICES.items()}


def save(values):
    """Write the given keys to .env (mode 600), keeping every other line. A blank value keeps what is set.
    Raises ValueError naming the first key whose value has the wrong shape (the value itself is never echoed)."""
    clean = {}
    for k, v in values.items():
        if k not in FIELDS:
            raise ValueError(f"{k}: 이 화면에서 바꿀 수 없는 설정이에요")
        v = (v or "").strip()
        if not v:
            continue
        # people paste "Header-Name: value" or quotes: keep only the value part
        v = re.sub(r"^[\w-]+:\s+", "", v).strip("'\" ")
        pattern, limit = FIELDS[k]
        if k == "CLOUD_URL" and not v.startswith("https://"):
            raise ValueError("Worker 주소: https://로 시작해야 해요 (wrangler deploy가 출력한 주소)")
        if k == "CLOUD_URL" and _internal(urlsplit(v).hostname or ""):
            raise ValueError("Worker 주소: 인터넷 주소여야 해요 (wrangler deploy가 출력한 workers.dev 주소)")
        if len(v) > limit or not re.fullmatch(pattern, v):
            raise ValueError(f"{LABEL[k]}: 형식이 맞지 않아요. 앞뒤 공백이나 다른 글자가 섞이지 않았는지 확인하세요")
        clean[k] = v
    if not clean:
        return []
    # A new Worker address gets the service token and the PC key in its headers (the check, every push), so it must
    # come with them: otherwise anything that can reach this page could redirect the stored secrets to its own server.
    old = config.load().get("CLOUD_URL", "").rstrip("/")
    if "CLOUD_URL" in clean and old and clean["CLOUD_URL"].rstrip("/") != old:
        need = [LABEL[k] for k in SERVICES["cloud"][1:] if k not in clean]
        if need:
            raise ValueError("Worker 주소를 바꿀 때는 " + ", ".join(need) + "도 함께 다시 넣어야 해요")
    path = config.ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and k in clean:
            lines[i] = f"{k}={clean[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in clean.items() if k not in seen]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")  # created 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return sorted(clean)


def _internal(host):
    """localhost or an IP literal that isn't a public address: never a Worker."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def public_ip():
    """This server's address on the internet, as the brokers see it (the one to put in an IP allowlist)."""
    for url in IP_SERVICES:
        try:
            ip = requests.get(url, timeout=5).text.strip()
        except requests.RequestException:
            continue
        if re.fullmatch(r"[\d.]{7,15}|[0-9a-fA-F:]{3,39}", ip):
            return ip
    raise RuntimeError("공인 IP를 알아내지 못했어요. 인터넷 연결을 확인하세요")


def new_pc_key():
    return secrets.token_urlsafe(32)


# (pattern in the error text, why it failed, what to do), first match wins; {ip} is this server's public IP
WHY = {
    "kis": (
        (r"EGW00133", "토큰을 1분 안에 다시 요청했어요 (한투는 1분에 한 번만 발급해요)", "1분 뒤에 다시 확인하세요"),
        (r"EGW00103|유효하지 않은 AppKey", "앱키가 올바르지 않아요", "KIS Developers > 내 앱에서 앱키를 다시 복사하세요"),
        (r"EGW00105|유효하지 않은 AppSecret", "앱시크릿이 올바르지 않아요", "앱시크릿 전체(아주 깁니다)를 다시 복사하세요"),
        (r"EGW0012[13]", "토큰이 거절됐어요", "잠시 뒤 다시 확인하세요. 계속되면 앱키를 재발급하세요"),
        (r"tokenP: HTTP 4\d\d", "한투가 토큰 발급을 거절했어요", "Open API 신청이 만료됐는지 확인하세요 (1년마다 연장)"),
        (r"CTRP6548R|OPSQ|계좌", "토큰은 받았지만 계좌 조회가 실패했어요",
         "계좌번호 앞 8자리와 상품코드(위탁계좌는 01)를 확인하세요. 그 계좌가 Open API에 등록돼 있어야 해요"),
    ),
    "toss": (
        (r"HTTP 403|access_denied|허용 IP", "이 서버의 IP({ip})가 토스에 등록돼 있지 않아요",
         "토스증권 PC 웹(WTS) 설정 > Open API > 허용 IP 관리에 {ip}를 등록하세요. 집 인터넷 IP는 가끔 바뀌어요"),
        (r"invalid_client|HTTP 401|HTTP 400", "Client ID나 Client Secret이 올바르지 않아요",
         "WTS 설정 > Open API에서 두 값을 다시 복사하세요. 재발급했다면 새 값이어야 해요"),
        (r"HTTP 429", "요청이 너무 잦아요", "1분 뒤 다시 확인하세요"),
    ),
    "cloud": (
        (r"must be https", "Worker 주소가 https로 시작하지 않아요", "wrangler deploy가 출력한 https://... 주소를 넣으세요"),
        (r"HTTP 30\d", "Cloudflare Access가 로그인 화면으로 돌려보냈어요 (서비스 토큰이 통과하지 못함)",
         "서비스 토큰 두 값을 확인하고, Worker의 Access 앱에 이 토큰을 포함한 Service Auth 정책이 있는지 보세요"),
        (r"X-Assetlog-Key", "PC 키가 Worker에 넣은 값과 달라요",
         "같은 값을 npx wrangler secret put PC_KEY로 Worker에도 넣으세요"),
        (r"PC_KEY is not set", "Worker에 PC 키가 없어요", "cloud/에서 npx wrangler secret put PC_KEY로 넣으세요"),
        (r"Access is not configured|HTTP 503", "Worker의 Access 설정(ACCESS_TEAM, ACCESS_AUD)이 비어 있어요",
         "./gaemi phone으로 다시 설정하거나, cloud/wrangler.toml의 ACCESS_TEAM·ACCESS_AUD를 채워 다시 배포하세요"),
        (r"HTTP 403", "Cloudflare가 요청을 거절했어요", "서비스 토큰과 Worker의 Access 정책을 확인하세요"),
        (r"HTTP 404", "그 주소에 개미장부 Worker가 없어요", "Worker 주소를 확인하세요"),
    ),
}
NETWORK = (r"ConnectionError|NameResolution|ConnectTimeout|ReadTimeout|Timeout|SSLError",
           "서버에 연결하지 못했어요", "이 기기의 인터넷 연결을 확인하세요")


def explain(service, error, ip="이 서버의 공인 IP"):
    for pattern, why, fix in (*WHY[service], NETWORK):
        if re.search(pattern, error):
            return why.format(ip=ip), fix.format(ip=ip)
    return "알 수 없는 이유로 실패했어요", "아래 오류 문구를 확인하세요"


def check(service):
    """One live round trip with what .env holds now: {ok, summary} or {ok: False, why, fix, error}."""
    from .brokers import kis, toss  # late: tests stub them
    cfg = config.load()
    missing = [LABEL[k] for k in SERVICES[service] if not cfg.get(k) and k != "KIS_ACNT_PRDT_CD"]
    if missing:
        return {"ok": False, "why": "아직 넣지 않은 값이 있어요", "fix": ", ".join(missing) + "을(를) 넣으세요", "error": ""}
    try:
        if service == "kis":
            total, _, _ = kis._snapshot(cfg)
            return {"ok": True, "summary": f"연결됐어요. 계좌 총자산 {total:,.0f}원"}
        if service == "toss":
            accounts = toss.holdings(cfg)
            return {"ok": True, "summary": f"연결됐어요. 계좌 {len(accounts)}개, 총자산 {sum(a['total_krw'] for a in accounts):,.0f}원"}
        return _check_cloud(cfg)
    except Exception as e:  # a failed check is the answer, not an error of this request
        error = f"{type(e).__name__}: {e}"[:400]
        ip = "이 서버의 공인 IP"
        if service == "toss":
            try:
                ip = public_ip()
            except RuntimeError:
                pass
        why, fix = explain(service, error, ip)
        return {"ok": False, "why": why, "fix": fix, "error": error}


def _check_cloud(cfg):
    from . import cloud
    url = cfg["CLOUD_URL"].rstrip("/")
    cloud._require_https(url)
    s = requests.Session()
    s.headers.update({"CF-Access-Client-Id": cfg["CF_ACCESS_CLIENT_ID"], "CF-Access-Client-Secret": cfg["CF_ACCESS_CLIENT_SECRET"],
                      "X-Assetlog-Key": cfg["CLOUD_PC_KEY"]})
    meta = cloud._call(s, "GET", f"{url}/api/meta")  # Access lets the service token through
    if meta.get("mode") != "cloud":
        raise RuntimeError("HTTP 404: not a gaemi Worker")
    # the PC key matches, and nothing is taken: past any op id (the Worker takes at most 15 digits)
    cloud._call(s, "GET", f"{url}/pc/ops", params={"after": 10**15 - 1})
    pushed = meta.get("pushed_at")
    return {"ok": True, "summary": "연결됐어요. " + (f"마지막으로 올린 시각 {pushed[:16].replace('T', ' ')}" if pushed
                                                   else "아직 올린 데이터가 없어요. 15분 안에 올라가요")}
