// setup.mjs against a fake Cloudflare API: what it sends, in what order, and how it explains refusals.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { CfError, client, create, inspect, mergeEnv, statements, verify, wranglerToml } from '../setup.mjs'

function fakeCloudflare(routes) {
  const calls = []
  const fetchImpl = async (url, opt) => {
    const path = url.replace('https://api.cloudflare.com/client/v4', '')
    const body = opt.body && JSON.parse(opt.body)
    calls.push([opt.method, path, body, opt.headers.Authorization])
    const hit = routes.find(([m, p]) => m === opt.method && (p instanceof RegExp ? p.test(path) : p === path))
    const [status, result] = hit ? hit[2](body, path) : [404, null]
    return { ok: status < 300, status, json: async () => status < 300 ? { success: true, result }
      : { success: false, errors: [{ code: status === 403 ? 10000 : 7003, message: 'nope' }] } }
  }
  return { calls, api: client('tok-123', fetchImpl) }
}

const ok = r => () => [200, r]

test('inspect: refuses a taken name, a missing subdomain or Zero Trust, and names the permission', async () => {
  const base = [['GET', '/accounts/A/workers/subdomain', ok({ subdomain: 'me' })],
    ['GET', '/accounts/A/access/organizations', ok({ auth_domain: 'blue-team.cloudflareaccess.com' })],
    ['GET', '/accounts/A/workers/scripts', ok([{ id: 'assetlog' }])],
    ['GET', '/accounts/A/d1/database?name=gaemi', ok([])]]
  assert.deepEqual(await inspect(fakeCloudflare(base).api, 'A', 'gaemi'), { team: 'blue-team', domain: 'gaemi.me.workers.dev' })
  await assert.rejects(inspect(fakeCloudflare(base).api, 'A', 'assetlog'), /이미 있어요. 덮어쓰지 않아요/)
  await assert.rejects(inspect(fakeCloudflare([['GET', '/accounts/A/workers/subdomain', () => [404, null]], ...base.slice(1)]).api, 'A', 'gaemi'),
    /workers.dev 주소가 아직 없어요/)
  await assert.rejects(inspect(fakeCloudflare([base[0], ['GET', '/accounts/A/access/organizations', () => [404, null]], ...base.slice(2)]).api, 'A', 'gaemi'),
    /Zero Trust가 아직/)
  await assert.rejects(inspect(fakeCloudflare([base[0], ['GET', '/accounts/A/access/organizations', () => [403, null]]]).api, 'A', 'gaemi'),
    e => e instanceof CfError && /Access: Organizations, Identity Providers, and Groups \/ Read/.test(e.message))
})

test('create: D1 and schema, then the token saved before anything else can fail, then Access, deploy, PC key', async () => {
  const { calls, api } = fakeCloudflare([
    ['POST', '/accounts/A/d1/database', ok({ uuid: 'db-uuid' })],
    ['POST', '/accounts/A/d1/database/db-uuid/query', ok([])],
    ['POST', '/accounts/A/access/service_tokens', ok({ id: 'tok-id', client_id: 'cid.access', client_secret: 'csecret' })],
    ['POST', '/accounts/A/access/policies', b => [200, { id: b.decision === 'allow' ? 'pol-login' : 'pol-pc' }]],
    ['POST', '/accounts/A/access/apps', ok({ id: 'app-id', aud: 'aud-tag' })],
    ['PUT', '/accounts/A/workers/scripts/gaemi/secrets', ok({ name: 'PC_KEY' })]])
  const saved = [], records = [], deployed = []
  const r = await create(api, 'A', { name: 'gaemi', email: 'me@example.com', team: 'blue-team', domain: 'gaemi.me.workers.dev',
    schema: readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'),
    save: v => saved.push(v), record: s => records.push({ ...s }), deploy: toml => deployed.push(toml) })
  const order = calls.map(([m, p]) => `${m} ${p.replace('/accounts/A', '')}`)
  assert.deepEqual(order, ['POST /d1/database', 'POST /d1/database/db-uuid/query', 'POST /d1/database/db-uuid/query',
    'POST /d1/database/db-uuid/query', 'POST /access/service_tokens', 'POST /access/policies', 'POST /access/policies',
    'POST /access/apps', 'PUT /workers/scripts/gaemi/secrets'])
  assert.ok(calls.every(c => c[3] === 'Bearer tok-123'))
  assert.equal(saved.length, 1)
  assert.deepEqual(Object.keys(saved[0]), ['CLOUD_URL', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET', 'CLOUD_PC_KEY'])
  assert.equal(saved[0].CF_ACCESS_CLIENT_SECRET, 'csecret')
  assert.equal(saved[0].CLOUD_URL, 'https://gaemi.me.workers.dev')
  const [, , login] = calls[5], [, , pc] = calls[6], [, , app] = calls[7], [, , secret] = calls[8]
  assert.deepEqual(login.include, [{ email: { email: 'me@example.com' } }])
  assert.deepEqual([pc.decision, pc.include], ['non_identity', [{ service_token: { token_id: 'tok-id' } }]])
  assert.deepEqual([app.type, app.domain, app.session_duration, app.policies],
    ['self_hosted', 'gaemi.me.workers.dev', '720h', [{ id: 'pol-login', precedence: 1 }, { id: 'pol-pc', precedence: 2 }]])
  assert.deepEqual([secret.name, secret.type, secret.text], ['PC_KEY', 'secret_text', saved[0].CLOUD_PC_KEY])
  assert.match(deployed[0], /^name = "gaemi"$/m)
  assert.match(deployed[0], /^database_id = "db-uuid"$/m)
  assert.match(deployed[0], /^ACCESS_TEAM = "blue-team"/m)
  assert.match(deployed[0], /^ACCESS_AUD = "aud-tag"/m)
  assert.deepEqual(records.at(-1), { accountId: 'A', name: 'gaemi', databaseId: 'db-uuid', serviceTokenId: 'tok-id',
    policyIds: ['pol-login', 'pol-pc'], appId: 'app-id', script: 'gaemi' })
  assert.equal(r.clientSecret, 'csecret')
})

test('a missing permission is explained, and what was made so far is recorded for --remove', async () => {
  const { api } = fakeCloudflare([['POST', '/accounts/A/d1/database', ok({ uuid: 'db-uuid' })],
    ['POST', '/accounts/A/d1/database/db-uuid/query', ok([])],
    ['POST', '/accounts/A/access/service_tokens', () => [403, null]]])
  const records = []
  await assert.rejects(create(api, 'A', { name: 'gaemi', email: 'a@b.co', team: 't', domain: 'd', schema: 'SELECT 1;',
    save: () => {}, record: s => records.push({ ...s }), deploy: () => {} }), /Access: Service Tokens \/ Edit/)
  assert.deepEqual(records.at(-1), { accountId: 'A', name: 'gaemi', databaseId: 'db-uuid' })
})

test('helpers: schema statements, .env merge, verify', async () => {
  assert.deepEqual(statements('-- c\nCREATE TABLE a (x);\n-- d\nCREATE INDEX i ON a(x);\n'), ['CREATE TABLE a (x)', 'CREATE INDEX i ON a(x)'])
  assert.equal(mergeEnv('# keys\nKIS_APP_KEY=k\nCLOUD_URL=old\n', { CLOUD_URL: 'new', CLOUD_PC_KEY: 'p' }),
    '# keys\nKIS_APP_KEY=k\nCLOUD_URL=new\nCLOUD_PC_KEY=p\n')
  assert.throws(() => wranglerToml('name = "x"\n', { name: 'a', databaseId: 'b', team: 'c', aud: 'd' }), /changed shape/)
  const seen = []
  const fetchImpl = async (url, opt) => { seen.push([url, opt.headers['CF-Access-Client-Id'], opt.redirect])
    return seen.length < 2 ? { status: 302, json: async () => ({}) } : { status: 200, json: async () => ({ mode: 'cloud' }) } }
  assert.equal(await verify('https://g.me.workers.dev', 'cid', 'sec', fetchImpl, 3, 1), true)
  assert.deepEqual(seen[0], ['https://g.me.workers.dev/api/meta', 'cid', 'manual'])
  await assert.rejects(verify('https://g', 'c', 's', async () => ({ status: 403, json: async () => ({}) }), 2, 1), /응답하지 않아요 \(HTTP 403\)/)
})

test('a wrong or malformed API token is named as such, not as a missing permission', async () => {
  for (const [status, errors] of [[403, [{ code: 9109, message: 'Invalid access token' }]],
    [400, [{ code: 6003, message: 'Invalid request headers' }]]]) {
    const api = client('x', async () => ({ ok: false, status, json: async () => ({ success: false, errors }) }))
    await assert.rejects(api('GET', '/accounts', undefined, 'Account Settings / Read'), /API 토큰이 올바르지 않아요/)
  }
})
