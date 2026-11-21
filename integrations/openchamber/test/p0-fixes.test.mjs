// Regression tests for the OpenChamber P0 audit fixes:
//  - the idle distill line writes the summary back through /add (sealed metadata)
//  - permanent /add rejections are held, not retried forever or dropped
//  - conversation text never reaches the service log
//  - the installer stores the token PATH, never the token itself
import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
// OpenChamber gives services a scrubbed environment; keep only what Node needs per OS.
const baseEnv = Object.fromEntries(['PATH', 'SystemRoot', 'TEMP', 'TMP'].filter(k => process.env[k]).map(k => [k, process.env[k]]))

async function freePort() {
  const temp = createServer()
  await new Promise(resolve => temp.listen(0, '127.0.0.1', resolve))
  const port = temp.address().port
  await new Promise(resolve => temp.close(resolve))
  return port
}

async function fakeApi(t, handler) {
  const received = []
  const api = createServer((req, res) => {
    let raw = ''
    req.on('data', d => { raw += d })
    req.on('end', () => {
      const entry = { url: req.url, body: raw ? JSON.parse(raw) : null, auth: req.headers.authorization }
      received.push(entry)
      const [status, body] = handler(entry)
      res.writeHead(status, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(body))
    })
  })
  await new Promise(resolve => api.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => api.close(() => resolve())))
  return { base: `http://127.0.0.1:${api.address().port}`, received }
}

async function startService(t, apiBase, extra = {}) {
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'aidumei-och-p0-'))
  const stateDir = path.join(scratch, '.aidumei-openchamber')
  await mkdir(stateDir, { recursive: true })
  const tokenFile = path.join(scratch, 'token')
  await writeFile(tokenFile, 'file-token\n')
  await writeFile(path.join(stateDir, 'config.json'), JSON.stringify({
    apiBase, apiTokenFile: tokenFile, userId: 'och-p0', bankId: 'och-p0',
    spoolDir: path.join(scratch, 'spool'), idleDistillMs: 200, allowedRoots: [os.tmpdir()], ...extra,
  }))
  const port = await freePort()
  let output = ''
  const spawnService = () => spawn(process.execPath, [path.join(here, '..', 'service', 'main.js')], {
    env: { ...baseEnv, HOME: scratch, OPENCHAMBER_SERVICE_PORT: String(port), OPENCHAMBER_SERVICE_TOKEN: 'svc' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let svc
  async function launch() {
    svc = spawnService()
    svc.stdout.on('data', d => { output += d })
    svc.stderr.on('data', d => { output += d })
    for (let i = 0; i < 40; i += 1) {
      try { if ((await fetch(`http://127.0.0.1:${port}/health`, { headers: { Authorization: 'Bearer svc' } })).ok) return } catch {}
      await new Promise(r => setTimeout(r, 100))
    }
    throw new Error(`service_start_failed: ${output}`)
  }
  async function stop() {
    if (svc.exitCode !== null || svc.signalCode !== null) return
    const stopped = new Promise(resolve => svc.once('exit', resolve))
    svc.kill()
    await stopped
  }
  t.after(async () => { await stop(); await rm(scratch, { recursive: true, force: true }) })
  await launch()
  return { port, scratch, stateDir, logs: () => output, stop, restart: async () => { await stop(); await launch() } }
}

async function turn(svc, sessionId, id, prompt, reply) {
  const prompts = path.join(svc.stateDir, 'prompts')
  await mkdir(prompts, { recursive: true })
  await writeFile(path.join(prompts, `${sessionId}.json`), JSON.stringify({ sessionId, text: prompt, at: Date.now() }))
  return fetch(`http://127.0.0.1:${svc.port}/session-events`, {
    method: 'POST',
    headers: { Authorization: 'Bearer svc', 'Content-Type': 'application/json' },
    body: JSON.stringify({
      event: { id, type: 'turn-finished', session: { id: sessionId }, project: { directory: os.tmpdir() } },
      content: { type: 'turn-finished', reply },
    }),
  })
}

async function waitFor(fn, ms = 6000) {
  const until = Date.now() + ms
  while (Date.now() < until) {
    const value = await fn()
    if (value) return value
    await new Promise(r => setTimeout(r, 100))
  }
  return fn()
}

test('idle distill writes the sealed summary back through /add', async t => {
  const api = await fakeApi(t, ({ url }) => {
    if (url.startsWith('/session/distill')) {
      return [200, { status: 'ok', summary: '精华一句', source_fingerprint: 'fp-1', metadata: { kind: 'session_distill', _distill_seal: 'sig' } }]
    }
    return [200, { status: 'ok' }]
  })
  const svc = await startService(t, api.base)
  assert.equal((await turn(svc, 'ses-a', 'evt-1', '问题', '回答')).status, 200)
  const summaryAdd = await waitFor(() => api.received.find(r => r.url === '/add' && r.body?.messages === '精华一句'))
  assert.ok(summaryAdd, `summary must be written: ${api.received.map(r => r.url).join(',')}`)
  assert.equal(summaryAdd.body.metadata._distill_seal, 'sig', 'server seal must travel unchanged')
  assert.equal(summaryAdd.body.metadata.session_id, 'distill:och-ses-a')
  assert.equal(summaryAdd.auth, 'Bearer file-token', 'token is read from the token file')
  const turnIdx = api.received.findIndex(r => r.url === '/add' && Array.isArray(r.body?.messages))
  const distillIdx = api.received.findIndex(r => r.url.startsWith('/session/distill'))
  assert.ok(turnIdx >= 0 && turnIdx < distillIdx, 'turns are delivered before the session is distilled')
})

test('permanent /add rejections are held for review and conversation text stays out of logs', async t => {
  const api = await fakeApi(t, ({ url }) => (url === '/add' ? [422, { detail: 'rejected' }] : [200, { status: 'skipped', reason: 'too_short' }]))
  const svc = await startService(t, api.base)
  await turn(svc, 'ses-b', 'evt-2', '秘密问题内容', '秘密回答内容')
  const pending = path.join(svc.scratch, 'spool', 'pending')
  const held = await waitFor(async () => (await readdir(pending).catch(() => [])).find(name => name.endsWith('.held')))
  assert.ok(held, 'a 422 must leave a .held job, not a silent retry loop')
  const payload = JSON.parse(await readFile(path.join(pending, held), 'utf8'))
  assert.equal(payload.messages[0].content, '秘密问题内容', 'held job keeps the exact payload')
  assert.doesNotMatch(svc.logs(), /秘密/, 'service logs must not contain conversation text')
})

test('installer stores the token path, never the token contents; check reports held jobs', async t => {
  const api = await fakeApi(t, () => [200, { status: 'ok', health_status: 'ok' }])
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'aidumei-och-manage-'))
  t.after(() => rm(scratch, { recursive: true, force: true }))
  const tokenFile = path.join(scratch, 'token')
  await writeFile(tokenFile, 'very-secret-token')
  const run = args => new Promise(resolve => {
    const child = spawn(process.execPath, [path.join(here, '..', 'manage.mjs'), ...args], { env: { ...baseEnv, HOME: scratch, USERPROFILE: scratch } })
    let out = ''
    child.stdout.on('data', d => { out += d })
    child.stderr.on('data', d => { out += d })
    child.on('close', code => resolve({ code, out }))
  })
  const common = ['--opencode-config-dir', path.join(scratch, 'oc'), '--state-dir', path.join(scratch, 'state')]
  const install = await run(['install', '--api', api.base, '--user', 'u', '--bank', 'b', '--token-file', tokenFile, ...common])
  assert.equal(install.code, 0, install.out)
  const config = await readFile(path.join(scratch, 'state', 'config.json'), 'utf8')
  assert.doesNotMatch(config, /very-secret-token/)
  assert.match(config, /apiTokenFile/)
  const clean = await run(['check', ...common])
  assert.equal(clean.code, 0, clean.out)
  const pending = path.join(scratch, 'state', 'spool', 'pending')
  await mkdir(pending, { recursive: true })
  await writeFile(path.join(pending, 'och-x.json.held'), '{}')
  const dirty = await run(['check', ...common])
  assert.equal(dirty.code, 1, 'held jobs must fail the check')
  assert.match(dirty.out, /"held":1/)
})

test('the service independently enforces an empty workspace allowlist', async t => {
  const api = await fakeApi(t, () => [200, { status: 'ok' }])
  const svc = await startService(t, api.base, { allowedRoots: [] })
  const response = await turn(svc, 'denied-session', 'denied-event', 'private question', 'private answer')
  assert.equal((await response.json()).skipped, 'workspace_or_child_session')
  assert.equal(api.received.length, 0)
  assert.equal((await readdir(path.join(svc.stateDir, 'prompts'))).length, 1, 'denied events must not consume files')
})

test('FIFO pairing preserves two prompts and event replay cannot consume the next one', async t => {
  const api = await fakeApi(t, () => [200, { status: 'ok' }])
  const svc = await startService(t, api.base, { idleDistillMs: 60000 })
  const prompts = path.join(svc.stateDir, 'prompts')
  const now = Date.now()
  await writeFile(path.join(prompts, 'one.json'), JSON.stringify({ sessionId: 'fifo', turnId: 'message-1', text: 'first question', at: now - 20 }))
  await writeFile(path.join(prompts, 'two.json'), JSON.stringify({ sessionId: 'fifo', turnId: 'message-2', text: 'second question', at: now - 10 }))
  const finish = (id, reply) => fetch(`http://127.0.0.1:${svc.port}/session-events`, {
    method: 'POST', headers: { Authorization: 'Bearer svc', 'Content-Type': 'application/json' },
    body: JSON.stringify({ event: { id, at: Date.now(), type: 'turn-finished', session: { id: 'fifo' }, project: { directory: os.tmpdir() } }, content: { type: 'turn-finished', reply } }),
  })
  assert.equal((await finish('event-1', 'first answer')).status, 200)
  assert.equal((await (await finish('event-1', 'first answer')).json()).replay, true)
  assert.equal((await readdir(prompts)).length, 1)
  assert.equal((await finish('event-2', 'second answer')).status, 200)
  await waitFor(() => api.received.filter(entry => entry.url === '/add').length === 2)
  const adds = api.received.filter(entry => entry.url === '/add')
  assert.deepEqual(adds.map(entry => entry.body.messages.map(message => message.content)), [['first question', 'first answer'], ['second question', 'second answer']])
  assert.ok(adds.every(entry => entry.body.metadata._origin_session_id === 'och-fifo'))
})

test('failed outbox persistence preserves the exact source prompt and reports failure', async t => {
  const api = await fakeApi(t, () => [200, { status: 'ok' }])
  const svc = await startService(t, api.base)
  const pending = path.join(svc.scratch, 'spool', 'pending')
  await rm(pending, { recursive: true })
  await writeFile(pending, 'an occupied path')
  const response = await turn(svc, 'persist-fail', 'event-fail', 'original question', 'answer')
  assert.equal(response.status, 500)
  const prompt = JSON.parse(await readFile(path.join(svc.stateDir, 'prompts', 'persist-fail.json'), 'utf8'))
  assert.equal(prompt.text, 'original question')
  assert.equal(api.received.length, 0)
})

test('idle distill plans survive a service restart before their deadline', async t => {
  const api = await fakeApi(t, ({ url }) => url.startsWith('/session/distill')
    ? [200, { status: 'ok', summary: 'recovered summary', source_fingerprint: 'recovered', metadata: { _distill_seal: 'sealed' } }]
    : [200, { status: 'ok' }])
  const svc = await startService(t, api.base, { idleDistillMs: 1000 })
  await turn(svc, 'restart-session', 'restart-event', 'question', 'answer')
  const plans = path.join(svc.scratch, 'spool', 'distill')
  assert.equal((await readdir(plans)).length, 1)
  await svc.restart()
  const add = await waitFor(() => api.received.find(entry => entry.url === '/add' && entry.body.messages === 'recovered summary'))
  assert.ok(add, 'the recovered plan must write the sealed summary')
  assert.equal(add.body.metadata._distill_seal, 'sealed')
  await waitFor(async () => (await readdir(plans)).length === 0)
  assert.deepEqual(await readdir(plans), [])
})
