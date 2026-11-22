"""`python -m assetlog setup`: the setup page as terminal questions, for a machine without a browser at hand
(a phone running Termux, a NAS over SSH). Same saving and checks as the page (setup.py); secrets are typed blind.
"""
import getpass

from . import config, setup

TITLE = {"kis": "한국투자증권", "toss": "토스증권", "cloud": "폰에서 보기 (Cloudflare, 선택)"}
SECRET = {"KIS_APP_KEY", "KIS_APP_SECRET", "TOSS_CLIENT_SECRET", "CF_ACCESS_CLIENT_SECRET", "CLOUD_PC_KEY"}


def _yes(q, default):
    a = input(f"{q} ({'Y/n' if default else 'y/N'}) ").strip().lower()
    return default if not a else a.startswith("y")


def _ask(key, is_set):
    prompt = f"  {setup.LABEL[key]}{' (설정됨, 그대로 두려면 Enter)' if is_set else ''}: "
    if key == "CLOUD_PC_KEY" and not is_set and _yes("  PC 키를 새로 만들까요?", True):
        value = setup.new_pc_key()
        print(f"  새 PC 키: {value}\n  Worker에도 같은 값을 넣으세요: cloud/에서 npx wrangler secret put PC_KEY")
        return value
    return (getpass.getpass(prompt) if key in SECRET else input(prompt)).strip()


def wizard():
    print(f"개미장부 연결 설정. 값은 {config.ENV_FILE}에만 저장돼요. 비밀 값은 입력해도 화면에 보이지 않아요.\n")
    try:
        print(f"이 서버의 공인 IP: {setup.public_ip()}\n  토스증권처럼 허용 IP를 쓰는 증권사에 이 주소를 등록하세요.\n")
    except RuntimeError as e:
        print(f"공인 IP: {e}\n")
    for svc, keys in setup.SERVICES.items():
        is_set = setup.status()[svc]
        if not _yes(f"[{TITLE[svc]}] {'다시 ' if all(is_set.values()) else ''}설정할까요?", not all(is_set.values()) and svc != "cloud"):
            continue
        while True:
            try:
                setup.save({k: _ask(k, is_set[k]) for k in keys})
            except ValueError as e:
                print(f"  저장하지 않았어요: {e}")
            else:
                print("  연결 확인 중…")
                r = setup.check(svc)
                if r["ok"]:
                    print(f"  성공: {r['summary']}\n")
                    break
                print(f"  실패: {r['why']}\n  해결: {r['fix']}" + (f"\n  오류 원문: {r['error']}" if r["error"] else ""))
            if not _yes("  다시 넣어 볼까요?", True):
                print()
                break
            is_set = setup.status()[svc]
    print("끝났어요. 증권사 동기화는 매일 23:50에 돌고, 지금 바로 하려면: python -m assetlog sync")
