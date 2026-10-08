---
name: gaemi-setup
description: Set up 개미장부 (gaemi), this repo's self-hosted Korean portfolio tracker, for the person at the keyboard. Covers Python deps, .env with their own broker API keys (KIS, Toss, optional KB), the first sync, the daily systemd timer, optional extras (DC pension model, savings, gold), and the optional phone copy on Cloudflare Workers behind Cloudflare Access. Use when the user asks to set up, install, configure, or deploy 개미장부/gaemi/assetlog, or to put it on their phone.
---

# 개미장부 setup

Walk the user through setup one step at a time, checking each step before the next. Speak the user's language (Korean by default). The README has the same steps for humans; this file adds the rules and checks you follow.

## Rules (never break these)

1. **Never ask for, accept, print, or log a secret.** API keys, app secrets, client secrets, `CLOUD_PC_KEY`, Cloudflare service-token values, tokens in `data/tokens.json`, cookies and JWTs stay out of the chat and out of your tool output.
   - The user types secrets into `.env` themselves, with an editor you open for them (for example `nano .env`).
   - If they paste one in chat anyway, tell them to rotate that key at the broker or Cloudflare. Do not repeat the value.
2. **Read `.env` only inside a process that prints names, lengths or booleans**, never values. Never `cat`, `diff`, `cp -r` or `rsync` the project in a way that dumps `.env`, `data/` or `cloud/wrangler.toml` into your context. Exclude them explicitly.
3. **`.env` must be mode 600.** Check it with `stat -c %a .env` after every edit.
4. **The app is read-only toward brokers.** Never add or call order, transfer or withdrawal endpoints.
5. **Cloudflare: touch only this app's own resources.** The user's account may host other Workers.
   - Before creating a Worker or a D1 database, check that the name is free. Never overwrite an existing one.
   - Never use "Protect all Workers" or an account-wide Access default.
   - Never use an Email-domain Access policy for a public domain such as gmail.com.
   - Never change DNS, the workers.dev subdomain, identity providers or other Access apps.
   - Show each Cloudflare-writing command before running it.
6. **Ask before anything outward-facing or irreversible.** That includes deploys, deleting Cloudflare resources, and `sudo`. Let the user run `sudo` commands themselves.
7. **KIS token issuance sends the user a KakaoTalk alert and is limited to about one per minute.** Don't loop on calls that issue tokens. The app caches tokens in `data/tokens.json`.

## Step 1: check the machine

- Linux with systemd user services (`systemctl --user`), Python 3.10+, and `pip`.
- Optional:
  - `pdftotext` (poppler) to import Toss statement PDFs.
  - Node.js 18+ for the phone copy.
- Run `pip install -r requirements.txt` (a virtualenv is fine).
- Run `python3 -m pytest -q`. It should pass; the UI tests are skipped without Chrome.

## Step 2: demo first (optional, recommended)

- Run `python3 -m assetlog demo`, then `ASSETLOG_DB=data/demo.db python3 -m assetlog serve`.
- Tell the user to open http://127.0.0.1:8000. Stop the server afterwards.

## Step 3: `.env`

- Run `cp .env.example .env && chmod 600 .env`.
- Explain which keys they need, and let them fill in the values themselves:
  - **한국투자증권 (KIS)**
    - Apply at https://apiportal.koreainvestment.com (KIS Developers). Fill in `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_CANO` (the first 8 digits of the account number) and `KIS_ACNT_PRDT_CD` (`01` for a 위탁 account).
    - The subscription lasts one year and must be renewed.
    - KIS keys can place orders, so they must stay private.
  - **토스증권**
    - Issue keys in the Toss WTS (PC web) under 설정 > Open API, giving `TOSS_CLIENT_ID` and `TOSS_CLIENT_SECRET`.
    - They must register this PC's public IP under 허용 IP 관리. To find it, run a short Python check that prints only `requests.get("https://api.ipify.org").text`.
    - Some workplaces send traffic out through several IPs. If Toss fails on one network, register the new IP.
    - Toss dividends are computed from KIS per-share data, so KIS keys are needed for them.
  - **KB증권**: experimental and never tested against a live account. Leave it empty unless the user insists.
- Validate without printing values. For example, run Python that loads `.env` and prints, for each expected key, whether it is set and its length. Header names pasted in by mistake (for example `CF-Access-Client-Id: ...`) show up as spaces in the value; detect that and ask the user to keep only the value.

## Step 4: first sync

- Run `python3 -m assetlog probe kis` (and `probe toss`). Probe output masks keys and tokens.
- Then run `python3 -m assetlog sync` and read the per-broker report.
  - `toss: ... 403 access_denied` means the IP isn't allowlisted (Step 3).
  - KIS `EGW00103`/`EGW00105` mean an invalid key. `EGW00133` means the token was requested too soon; wait a minute.
- Run `python3 -m assetlog serve` and have the user check the 홈 and 보유 tabs at http://127.0.0.1:8000.
- Ask the user for each account's value at last year-end. They enter it in 입력 > 평가액 직접 입력, and the returns need it.

## Step 5: daily timer

- Run `./deploy/install.sh`. It installs `assetlog-sync.timer` (23:50 KST). If `CLOUD_URL` is set, it also installs `assetlog-cloud.timer` (every 15 minutes).
- For the timers to run without a login, the user runs `sudo loginctl enable-linger $USER` themselves.
- Check with `systemctl --user list-timers --all | grep assetlog`.

## Step 6: optional extras (only if the user has them)

- **DC pension without an API that buys one KRX ETF each month.**
  - Set `DC_ACCOUNT` (an account id they create in 입력 > 계좌 추가), `DC_SYMBOL` and `DC_START=YYYY-MM-DD,shares,cash`. `DC_START` is a day the share count is known exactly.
  - Set `DC_DAY` (the contribution day, default 25) and `DC_BUY_LAG` (the trading days until the buy, default 5).
  - The model needs KIS keys for prices.
- **Savings at principal only (예금·적금):** create an account with institution `manual` and put its id in `ASSETLOG_PRINCIPAL_ONLY`. The user then enters the 납입원금 total now and then.
- **Gold spot (e.g. KB 금현물):** create an account and put its id in `ASSETLOG_GOLD_ACCOUNT`. The user enters grams and total cost in 입력 > 금현물 보유 입력. The price is the KRX gold close (code M04020000) via KIS.
- **Asset classes in the 보유 donut:** unknown symbols count as 주식. Add bond, gold or crypto ETFs to `CLASS` in `assetlog/classes.py`.

## Step 7: phone copy on Cloudflare (optional)

Everything runs on the Cloudflare free plan.
- Turning on Zero Trust for the first time may ask for a card even on the free plan. It isn't charged.
- If the user refuses that, stop here: the local app works without the phone copy.

Work in `cloud/`.

1. **Install and log in.**
   - Run `npm install`, then `npx wrangler login`. The user approves this in the browser.
   - If wrangler lists several accounts, confirm which one to use.
2. **Check that the names are free.**
   - Run `npx wrangler d1 list`.
   - The Worker name must not already exist in the account. If `assetlog` is taken, change `name` in `wrangler.toml` and use another D1 name.
3. **Create the config and the database.**
   - Run `cp wrangler.toml.example wrangler.toml`.
   - Show the command, then run `npx wrangler d1 create assetlog` and put the printed `database_id` in `wrangler.toml`.
   - Show the command, then run `npx wrangler d1 execute assetlog --remote --file schema.sql`. If it fails with auth error 10000, use `--command "$(cat schema.sql)"` instead.
4. **PC key.**
   - Generate it in a process that writes it to `.env` as `CLOUD_PC_KEY` without printing it. Then pipe it to `npx wrangler secret put PC_KEY` from that same process, again without printing it.
5. **First deploy.**
   - Show the command, then run `npx wrangler deploy`.
   - Put the printed `https://<name>.<subdomain>.workers.dev` in `.env` as `CLOUD_URL`.
   - Every request now answers 503 until Access is configured. That is expected.
6. **The user, in the Cloudflare dashboard:**
   - Open the Worker, then the Access tab, then **Protect this Worker behind Access**. Choose All traffic, the policy **Cloudflare account**, and a session of 1 month.
   - Create a service token under Zero Trust > Access controls > Service credentials > Service Tokens.
   - Paste only the values into `.env` as `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`.
   - Add a policy to this Worker's Access application with action **Service Auth**, including that service token.
7. **Team and AUD.**
   - In a process, send a request to `CLOUD_URL` with the two service-token headers and decode the `CF_Authorization` cookie's JWT payload.
   - Print only `iss` and `aud`. The team is the subdomain of `iss` (`https://<team>.cloudflareaccess.com`), and `aud` is the AUD tag.
   - Put both in `wrangler.toml` (`ACCESS_TEAM`, `ACCESS_AUD`). Then show the command and run `npx wrangler deploy` again.
8. **Verify.**
   - A request without credentials must redirect to `*.cloudflareaccess.com`.
   - With the service token, `GET /api/meta` must answer 200.
   - `python3 -m assetlog cloud` must succeed.
   - Then re-run `./deploy/install.sh` so the 15-minute timer turns on.
9. **Phone.** The user opens `CLOUD_URL`, logs in with their Cloudflare account, and taps 홈 화면에 추가.

**To remove it:**
- Run `systemctl --user disable --now assetlog-cloud.timer` and clear `CLOUD_URL`.
- Run `npx wrangler delete` and `npx wrangler d1 delete <db>`.
- The user deletes the Access application and the service token in Zero Trust.

## Done checklist

- `.env` is mode 600, and the `.gitignore` entries are still in place.
- `python3 -m assetlog sync` has no errors, or only ones the user accepted, such as Toss on a network that isn't allowlisted.
- Timers are listed in `systemctl --user list-timers`.
- If the phone copy is on, the phone shows "PC 동기화 기준" with a recent time.
