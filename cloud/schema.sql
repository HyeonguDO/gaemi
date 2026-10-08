-- D1 schema. Local: npx wrangler d1 execute assetlog --local --file schema.sql  (--remote for the real one)
-- state: what the PC last pushed (PUT /pc/state), one JSON document per read API.
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- ops: the phone's writes, queued until the PC pulls and applies them (GET /pc/ops, POST /pc/ops/<id>).
CREATE TABLE IF NOT EXISTS ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  query TEXT,
  content_type TEXT,
  filename TEXT,
  body BLOB,
  status TEXT NOT NULL DEFAULT 'queued',
  result TEXT,
  done_at TEXT
);
-- ops is never pruned: without this, the queued count, the last-10 errors, the pull and the sync dedup scan every op
CREATE INDEX IF NOT EXISTS ops_status ON ops(status, id);
