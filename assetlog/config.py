"""Settings from environment, overlaid on a local .env file (KEY=VALUE lines)."""
import fcntl
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(path=ROOT / ".env"):
    cfg = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    prefixes = ("KB_", "KIS_", "TOSS_", "ASSETLOG_", "DC_", "CLOUD_", "CF_")
    cfg.update({k: v for k, v in os.environ.items() if k.startswith(prefixes)})
    return cfg


def cached_token(name, issue, margin=600):
    """Reuse broker tokens until shortly before expiry (KIS throttles re-issuing).
    issue() -> (token, lifetime_seconds). Stored owner-only in data/tokens.json.
    margin=float("inf") forces a new token. The file lock matters for Toss, which keeps one valid
    token per client: a cron sync and the UI's sync button issuing at once would revoke each other."""
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "tokens.json"
    path.touch(mode=0o600, exist_ok=True)
    with open(DATA / "tokens.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tokens = json.loads(path.read_text() or "{}")
        hit = tokens.get(name)
        if hit and hit["expires"] - margin > time.time():
            return hit["token"]
        token, lifetime = issue()
        tokens[name] = {"token": token, "expires": time.time() + lifetime}
        path.write_text(json.dumps(tokens))
        return token
