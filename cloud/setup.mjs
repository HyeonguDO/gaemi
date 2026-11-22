#!/usr/bin/env node
// Sets up the phone copy (this Worker, its D1 database and Cloudflare Access) with one Cloudflare API token, then
// writes CLOUD_URL, the service token and the PC key into the app's .env. Run in this directory:
//   node setup.mjs                      create everything, asking before any write
//   node setup.mjs --env ../data/.env   where the app's .env is (default: ../data/.env if it exists, else ../.env)
//   node setup.mjs --print              print the four .env lines instead of writing them (a phone that runs the app)
//   node setup.mjs --remove             delete what this script created (ids in .gaemi-cloud.json)
// It only creates resources under the name you choose and refuses a name already taken; it never changes other
// Workers, DNS or login methods. A missing workers.dev subdomain it creates (after asking); the one thing it can't do
// is turn on Zero Trust (choosing the free plan is a dashboard step): it checks and says what to do.
import { spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { chmodSync, existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const STATE = join(HERE, '.gaemi-cloud.json')  // ids of what was created (no secrets), for --remove
export const API = 'https://api.cloudflare.com/client/v4'
// The token's permissions, prefilled through the dashboard's template URL (developers.cloudflare.com/fundamentals/
// api/how-to/account-owned-token-template/): Zero Trust Edit covers Access apps, policies, service tokens and the
// organization read.
const KEYS = [['workers_scripts', 'edit'], ['d1', 'edit'], ['teams', 'edit'], ['account_settings', 'read']]
export const PERMISSIONS = ['Workers Scripts / Edit', 'D1 / Edit', 'Zero Trust / Edit', 'Account Settings / Read']
export const TOKEN_URL = 'https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys=' +
  encodeURIComponent(JSON.stringify(KEYS.map(([key, type]) => ({ key, type })))) + '&accountId=*&zoneId=all&name=gaemi-setup'

// What each step needs, to say why a 403 happened (Cloudflare answers code 10000 for a missing permission)
const NEEDS = { d1: 'D1 / Edit', scripts: 'Workers Scripts / Edit', access: 'Zero Trust / Edit',
  tokens: 'Zero Trust / Edit', org: 'Zero Trust / Edit' }

export class CfError extends Error {}

export function client(token, fetchImpl = fetch) {
  return Object.assign(async (method, path, body, need) => {
    let r
    try {
      r = await fetchImpl(API + path, { method, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body) })
    } catch (e) {
      throw new CfError(`Cloudflare에 연결하지 못했어요 (${e.message}). 인터넷 연결을 확인하세요`)
    }
    const data = await r.json().catch(() => ({}))
    if (r.ok && data.success !== false) return data.result
    const errs = (data.errors || []).map(e => `${e.code} ${e.message}`).join('; ') || `HTTP ${r.status}`
    if (/\b(9109|6003|6111|1000) /.test(errs)) {  // seen live 2026-11-22: 9109 for a wrong token, 6003/6111 for a malformed one
      throw new CfError(`API 토큰이 올바르지 않아요 (${errs}). 토큰 전체를 다시 복사하세요. 만든 직후 한 번만 보여요`)
    }
    if (r.status === 401 || r.status === 403 || /\b(10000|10001)\b/.test(errs)) {
      throw new CfError(`권한이 없어요 (${errs}). API 토큰에 "${NEEDS[need] || need}" 권한이 있는지 확인하세요`)
    }
    throw new CfError(`${method} ${path.replace(/[0-9a-f]{32}/g, '<id>')}: ${errs}`)
  }, { token })  // wrangler deploy gets the same token
}

// Read-only checks first: what exists, and whether the account is ready. Returns what the plan needs.
export async function inspect(api, accountId, name, chooseSubdomain = async () => null) {
  let sub = (await api('GET', `/accounts/${accountId}/workers/subdomain`, undefined, 'scripts').catch(e => {
    if (/권한/.test(e.message)) throw e
    return null  // no workers.dev subdomain chosen yet
  }))?.subdomain
  if (!sub) {  // a new account: pick the <name>.workers.dev every Worker of the account lives under
    const want = await chooseSubdomain()
    if (!want) throw new CfError('workers.dev 주소가 아직 없어요. 대시보드 > Workers & Pages에 한 번 들어가 workers.dev 이름을 정하세요')
    sub = (await api('PUT', `/accounts/${accountId}/workers/subdomain`, { subdomain: want }, 'scripts')).subdomain || want
  }
  let org
  try {
    org = await api('GET', `/accounts/${accountId}/access/organizations`, undefined, 'org')
  } catch (e) {
    if (/권한/.test(e.message)) throw e
    org = null
  }
  if (!org?.auth_domain) throw new CfError('Zero Trust가 아직 켜져 있지 않아요. https://one.dash.cloudflare.com 에서 팀 이름을 정하고 Free 플랜을 고른 뒤 다시 실행하세요(카드 등록을 요구할 수 있지만 청구되지 않아요)')
  const scripts = await api('GET', `/accounts/${accountId}/workers/scripts`, undefined, 'scripts')
  if (scripts.some(s => s.id === name)) throw new CfError(`'${name}' 이름의 Worker가 이미 있어요. 덮어쓰지 않아요: 다른 이름을 고르세요`)
  const dbs = await api('GET', `/accounts/${accountId}/d1/database?name=${encodeURIComponent(name)}`, undefined, 'd1')
  if (dbs.some(d => d.name === name)) throw new CfError(`'${name}' 이름의 D1 데이터베이스가 이미 있어요. 다른 이름을 고르세요`)
  return { team: org.auth_domain.replace(/\.cloudflareaccess\.com$/, ''), domain: `${name}.${sub}.workers.dev` }
}

export function statements(sql) {  // schema.sql split for the query endpoint, comments dropped
  return sql.split('\n').filter(l => !l.trim().startsWith('--')).join('\n').split(';').map(s => s.trim()).filter(Boolean)
}

export function wranglerToml(example, { name, databaseId, team, aud }) {
  const out = example
    .replace(/^name = ".*"/m, `name = "${name}"`)
    .replace(/^database_name = ".*"/m, `database_name = "${name}"`)
    .replace(/^database_id = .*$/m, `database_id = "${databaseId}"`)  // the example's placeholder note goes too
    .replace(/^ACCESS_TEAM = ""/m, `ACCESS_TEAM = "${team}"`)
    .replace(/^ACCESS_AUD = ""/m, `ACCESS_AUD = "${aud}"`)
  for (const v of [name, databaseId, team, aud]) if (!out.includes(`"${v}"`)) throw new Error('wrangler.toml.example changed shape')
  return out
}

// KEY=value lines in, kept in order; given keys replaced or appended; written 0600 through a temp file
export function mergeEnv(text, values) {
  const lines = text ? text.replace(/\n$/, '').split('\n') : [], seen = new Set()
  for (let i = 0; i < lines.length; i++) {
    const k = lines[i].split('=')[0].trim()
    if (lines[i].includes('=') && !lines[i].trimStart().startsWith('#') && k in values) { lines[i] = `${k}=${values[k]}`; seen.add(k) }
  }
  for (const [k, v] of Object.entries(values)) if (!seen.has(k)) lines.push(`${k}=${v}`)
  return lines.join('\n') + '\n'
}

function writeEnv(path, values) {
  const tmp = `${path}.${process.pid}.tmp`
  writeFileSync(tmp, mergeEnv(existsSync(path) ? readFileSync(path, 'utf8') : '', values), { mode: 0o600 })
  chmodSync(tmp, 0o600)
  renameSync(tmp, path)
}

// The writes, in an order that never loses a secret: the service token's secret goes to .env the moment it exists.
export async function create(api, accountId, { name, email, team, domain, schema, save, record, deploy }) {
  const done = { accountId, name }
  const step = (msg) => console.log(`- ${msg}`)
  step(`D1 데이터베이스 '${name}' 만들기`)
  const db = await api('POST', `/accounts/${accountId}/d1/database`, { name }, 'd1')
  done.databaseId = db.uuid; record(done)
  for (const sql of statements(schema)) await api('POST', `/accounts/${accountId}/d1/database/${db.uuid}/query`, { sql }, 'd1')
  step(`서비스 토큰 '${name}-pc' 만들기 (PC가 폰 사본에 올릴 때 쓰는 열쇠)`)
  const tok = await api('POST', `/accounts/${accountId}/access/service_tokens`, { name: `${name}-pc`, duration: '8760h' }, 'tokens')
  done.serviceTokenId = tok.id; record(done)
  const pcKey = randomBytes(32).toString('base64url')
  save({ CLOUD_URL: `https://${domain}`, CF_ACCESS_CLIENT_ID: tok.client_id, CF_ACCESS_CLIENT_SECRET: tok.client_secret, CLOUD_PC_KEY: pcKey })
  step(`Access 정책 두 개 만들기 (${email}의 로그인, 서비스 토큰)`)
  const login = await api('POST', `/accounts/${accountId}/access/policies`,
    { name: `${name} owner`, decision: 'allow', include: [{ email: { email } }] }, 'access')
  done.policyIds = [login.id]; record(done)
  const service = await api('POST', `/accounts/${accountId}/access/policies`,
    { name: `${name} pc`, decision: 'non_identity', include: [{ service_token: { token_id: tok.id } }] }, 'access')
  done.policyIds.push(service.id); record(done)
  step(`Access 앱 만들기 (${domain} 전체를 로그인 뒤에, 세션 30일)`)
  const app = await api('POST', `/accounts/${accountId}/access/apps`, { type: 'self_hosted', name, domain,
    session_duration: '720h', app_launcher_visible: false,
    policies: [{ id: login.id, precedence: 1 }, { id: service.id, precedence: 2 }] }, 'access')
  done.appId = app.id; record(done)
  if (!app.aud) throw new CfError('Access 앱의 AUD를 받지 못했어요')
  step('Worker 배포 (npx wrangler deploy)')
  deploy(wranglerToml(readFileSync(join(HERE, 'wrangler.toml.example'), 'utf8'), { name, databaseId: db.uuid, team, aud: app.aud }))
  done.script = name; record(done)
  step('PC 키를 Worker의 비밀값으로 넣기')
  await api('PUT', `/accounts/${accountId}/workers/scripts/${name}/secrets`, { name: 'PC_KEY', text: pcKey, type: 'secret_text' }, 'scripts')
  return { url: `https://${domain}`, clientId: tok.client_id, clientSecret: tok.client_secret, pcKey }
}

export async function verify(url, clientId, clientSecret, fetchImpl = fetch, tries = 12, wait = 5000) {
  let last = ''
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetchImpl(`${url}/api/meta`, { redirect: 'manual',
        headers: { 'CF-Access-Client-Id': clientId, 'CF-Access-Client-Secret': clientSecret } })
      if (r.status === 200 && (await r.json()).mode === 'cloud') return true
      last = `HTTP ${r.status}`
    } catch (e) { last = e.message }
    await new Promise(res => setTimeout(res, wait))  // Access and a new workers.dev name take a little while
  }
  throw new CfError(`만든 폰 사본이 1분 동안 응답하지 않아요 (${last}). 잠시 뒤 다시 확인하세요`)
}

async function ask(q, { hidden = false, def = '' } = {}) {
  if (!hidden) {
    const rl = createInterface({ input: process.stdin, output: process.stdout })
    const a = (await rl.question(q)).trim()
    rl.close()
    return a || def
  }
  process.stdout.write(q)
  return new Promise(res => {  // typed blind: the token never shows on the screen
    let s = ''
    const stdin = process.stdin
    stdin.setRawMode?.(true); stdin.resume()
    const on = d => {
      for (const c of d.toString('utf8')) {
        if (c === '\r' || c === '\n') { stdin.setRawMode?.(false); stdin.pause(); stdin.off('data', on); process.stdout.write('\n'); return res(s.trim()) }
        if (c === '\u0003') process.exit(130)
        s = c === '\u007f' ? s.slice(0, -1) : s + c
      }
    }
    stdin.on('data', on)
  })
}

async function pickAccount(api) {
  const accounts = await api('GET', '/accounts', undefined, 'Account Settings / Read')
  if (!accounts.length) throw new CfError('이 토큰으로 보이는 계정이 없어요. 토큰의 Account Resources에 계정을 넣으세요')
  if (accounts.length === 1) return accounts[0]
  accounts.forEach((a, i) => console.log(`  ${i + 1}. ${a.name}`))
  return accounts[Number(await ask('어느 계정에 만들까요? 번호: ')) - 1] || pickAccount(api)
}

function envPath(argv) {
  const i = argv.indexOf('--env')
  if (i >= 0) return argv[i + 1]
  return existsSync(join(HERE, '..', 'data', '.env')) ? join(HERE, '..', 'data', '.env') : join(HERE, '..', '.env')
}

async function remove(api) {
  if (!existsSync(STATE)) throw new CfError('.gaemi-cloud.json이 없어요: 이 스크립트로 만든 게 없거나 다른 기기에서 만들었어요')
  const s = JSON.parse(readFileSync(STATE, 'utf8'))
  console.log(`지울 것: Worker '${s.name}', D1 '${s.name}', Access 앱과 정책 ${s.policyIds?.length || 0}개, 서비스 토큰. 되돌릴 수 없어요.`)
  if (await ask(`정말 지우려면 이름(${s.name})을 그대로 입력하세요: `) !== s.name) return console.log('취소했어요')
  const a = `/accounts/${s.accountId}`
  const tries = [s.appId && ['DELETE', `${a}/access/apps/${s.appId}`, 'access'],
    ...(s.policyIds || []).map(id => ['DELETE', `${a}/access/policies/${id}`, 'access']),
    s.serviceTokenId && ['DELETE', `${a}/access/service_tokens/${s.serviceTokenId}`, 'tokens'],
    s.script && ['DELETE', `${a}/workers/scripts/${s.script}`, 'scripts'],
    s.databaseId && ['DELETE', `${a}/d1/database/${s.databaseId}`, 'd1']].filter(Boolean)
  for (const [m, p, need] of tries) {
    try { await api(m, p, undefined, need); console.log(`- 지웠어요: ${p.split('/').slice(-2).join('/')}`) } catch (e) { console.log(`- 못 지웠어요: ${e.message}`) }
  }
  renameSync(STATE, `${STATE}.removed`)
}

function openBrowser(url) {  // best effort: the link is printed anyway
  const cmd = process.platform === 'darwin' ? ['open', [url]] : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]] : ['xdg-open', [url]]
  try { spawnSync(cmd[0], cmd[1], { stdio: 'ignore', timeout: 5000 }) } catch {}
}

async function main(argv) {
  console.log('개미장부 폰 사본 설정 (Cloudflare 무료 플랜)\n')
  console.log('Cloudflare API 토큰이 필요해요. 아래 링크를 열면 필요한 권한이 미리 채워진 토큰 만들기 화면이 나와요.')
  console.log('맨 아래 "요약 계속" > "토큰 생성"을 누르고, 나온 토큰을 복사해 여기에 붙여 넣으세요.\n')
  console.log(`  ${TOKEN_URL}\n`)
  console.log(`  (권한: ${PERMISSIONS.join(', ')}. 토큰은 저장하지 않아요. 끝나면 대시보드에서 지워도 돼요)\n`)
  openBrowser(TOKEN_URL)
  const api = client(await ask('API 토큰 (입력해도 화면에 안 보여요): ', { hidden: true }))
  if (argv.includes('--remove')) return remove(api)
  if (existsSync(join(HERE, 'wrangler.toml'))) throw new CfError('이미 이 폴더에 폰 사본 설정(wrangler.toml)이 있어요. 새로 만들려면 그 파일을 다른 이름으로 옮긴 뒤 다시 실행하세요')
  const account = await pickAccount(api)
  const name = await ask('Worker 이름 (영문 소문자·숫자·-, 기본 gaemi): ', { def: 'gaemi' })
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(name)) throw new CfError('Worker 이름은 영문 소문자, 숫자, -만 쓸 수 있어요')
  const email = await ask('폰에서 로그인할 이메일 (Cloudflare 계정 이메일): ')
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new CfError('이메일 형식이 아니에요')
  console.log('\n확인 중…')
  const plan = await inspect(api, account.id, name, async () => {
    const def = account.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 30) || 'gaemi'
    const sub = await ask(`이 계정은 workers.dev 주소가 아직 없어요. 쓸 이름 (모든 Worker 주소가 <이름>.workers.dev가 돼요, 기본 ${def}): `, { def })
    return /^[a-z0-9][a-z0-9-]{0,62}$/.test(sub) ? sub : null
  })
  console.log(`\n계정 '${account.name}'에 다음을 만들어요. 다른 Worker나 계정 설정은 건드리지 않아요.`)
  console.log(`  주소 https://${plan.domain}, D1 '${name}', 서비스 토큰 '${name}-pc', Access 앱 '${name}'(로그인 ${email})`)
  const print = argv.includes('--print'), env = envPath(argv)
  console.log(print ? '  키 네 줄은 마지막에 화면에 보여 줘요 (--print)' : `  키 네 줄은 ${env}에 저장해요`)
  if (!/^y/i.test(await ask('진행할까요? (y/N) '))) return console.log('취소했어요')
  if (!existsSync(join(HERE, 'node_modules', 'wrangler'))) spawnSync('npm', ['install', '--no-fund', '--no-audit'], { cwd: HERE, stdio: 'inherit' })
  let saved = {}
  const result = await create(api, account.id, { name, email, team: plan.team, domain: plan.domain,
    schema: readFileSync(join(HERE, 'schema.sql'), 'utf8'),
    save: v => { saved = v; if (!print) writeEnv(env, v) },
    record: s => writeFileSync(STATE, JSON.stringify(s, null, 2)),
    deploy: toml => {
      writeFileSync(join(HERE, 'wrangler.toml'), toml)
      const r = spawnSync('npx', ['wrangler', 'deploy'], { cwd: HERE, stdio: 'inherit',
        env: { ...process.env, CLOUDFLARE_API_TOKEN: api.token, CLOUDFLARE_ACCOUNT_ID: account.id } })
      if (r.status !== 0) throw new CfError('wrangler deploy가 실패했어요. 위 오류를 확인하세요 (토큰의 Workers Scripts / Edit 권한)')
    } })
  console.log('\n연결 확인 중…')
  await verify(result.url, result.clientId, result.clientSecret)
  console.log(`\n완료: https://${plan.domain}`)
  if (print) {
    console.log('\n아래 네 줄을 앱의 .env에 넣으세요 (폰이라면 gaemi.sh setup에 하나씩). 남에게 보여 주지 마세요:')
    for (const [k, v] of Object.entries(saved)) console.log(`${k}=${v}`)
  }
  console.log('\n폰에서: 위 주소를 열고 Cloudflare 계정으로 로그인한 뒤 "홈 화면에 추가". 데이터는 앱이 15분 안에 올려요.')
  console.log('지우려면: node setup.mjs --remove')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(e => {
    console.error(`\n실패: ${e instanceof CfError ? e.message : e.stack}`)
    if (existsSync(STATE)) console.error('만들다 만 것은 node setup.mjs --remove로 지울 수 있어요')
    process.exit(1)
  })
}
