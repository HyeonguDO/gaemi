# AGENTS.md

Instructions for AI coding agents (Codex, Cursor, Claude Code and others) working in this repository.

## What this is

개미장부 (gaemi) is a self-hosted portfolio tracker for Korean brokerage accounts. Python package `assetlog/` (FastAPI + SQLite + one-file web UI in `assetlog/static/`), optional Cloudflare Worker in `cloud/`. See README.md.

## Setting it up for a user

If the user asks to set up, install, configure or deploy this app, follow `.claude/skills/gaemi-setup/SKILL.md` step by step. That file is written for any agent, not only Claude Code.

## Hard rules

- Never ask for, accept, print or log secrets. That covers `.env` values, `data/tokens.json`, Cloudflare service tokens, cookies and JWTs. The user types secrets into `.env` (mode 600) themselves. Read `.env` only in code that prints names, lengths or booleans.
- Never dump `.env`, `data/`, `inbox/` or `cloud/wrangler.toml` into your context or into a commit. When diffing or copying the project, exclude them explicitly. They are git-ignored; keep it that way.
- The app is read-only toward brokers. Do not add or call order, transfer or withdrawal APIs.
- KIS token issuance alerts the user by KakaoTalk and is rate-limited. Reuse the cached token; never loop on token calls.
- Cloudflare: touch only this app's own Worker, D1 database and Access application. Never use "Protect all Workers", account-wide defaults, DNS or other Workers. Ask before deploying.
- Tests must never call brokers or Cloudflare. `tests/conftest.py` stubs the config.

## Working on the code

- Tests: `python3 -m pytest -q`. For the Worker, run `npm test` in `cloud/`; it runs `wrangler dev --local` against a throwaway database.
- Match the surrounding style: concise English comments, Korean UI strings.
- After changing `assetlog/static/` or `cloud/src/`, the user must redeploy the Worker (`npx wrangler deploy` in `cloud/`) for the phone copy.
