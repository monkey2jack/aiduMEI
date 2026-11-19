import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

/** Spawn the service with a scratch config and a fake aiduMEI API; return handles. */
async function startService(t, { events = [] } = {}) {
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'aidumei-och-test-'))
  const spoolDir = path.join(scratch, 'spool')
  const stateDir = path.join(scratch, '.aidumei-openchamber')
  await mkdir(stateDir, { recursive: true })
  const received = []
  const seenIds = new Set()

  // Fake aiduMEI API: records /add payloads, answers /search and /health.
  const api = createServer((req, res) => {
    let raw = ''
    req.on('data', d => { raw += d })
    req.on('end', () => {
      received.push({ url: req.url, body: raw })
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(req.url.startsWith('/search') ? { results: [{ memory: 'test recall' }] } : { status: 'ok' }))
    })
  })
  await new Promise(resolve => api.listen(0, '127.0.0.1', resolve))
  const apiPort = api.address().port

  // Write the service config.
  const config = { apiBase: `http://127.0.0.1:${apiPort}`, apiToken: 'test-token', userId: 'och-test', bankId: 'och-test', testMode: false, spoolDir, idleDistillMs: 300 }
  await writeFile(path.join(stateDir, 'config.json'), JSON.stringify(config))

  // Allocate a free port first, then spawn the service once with it.
  const temp = createServer()
  await new Promise(resolve => temp.listen(0, '127.0.0.1', resolve))
  const svcPort = temp.address().port
  temp.close(() => {})
  const svc = spawn(process.execPath, [path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'service', 'main.js')], {
    env: { ...process.env, HOME: scratch, OPENCHAMBER_SERVICE_PORT: String(svcPort), OPENCHAMBER_SERVICE_TOKEN: "svc-tok" },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  t.after(async () => { svc.kill(); await new Promise(r => { api.close(() => r()) }); await rm(scratch, { recursive: true, force: true }) })
  // Wait for health.
  for (let i = 0; i < 20; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${svcPort}/health`, { headers: { Authorization: 'Bearer svc-tok' } })
      if (r.ok) break
    } catch {}
    await new Promise(r => setTimeout(r, 200))
  }
  return { port: svcPort, received, spoolDir, scratch, api }
}

function postEvent(port, payload) {
  return fetch('http://127.0.0.1:{{PORT}}/session-events'.replace('{{PORT}}', String(port)), {
    method: 'POST',
    headers: { Authorization: 'Bearer svc-tok', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

test('service health endpoint answers with bearer', async t => {
  const { port } = await startService(t)
  const r = await fetch(`http://127.0.0.1:${port}/health`, { headers: { Authorization: 'Bearer svc-tok' } })
  assert.equal(r.status, 200)
  const body = await r.json()
  assert.equal(body.ok, true)
})

test('service rejects missing bearer', async t => {
  const { port } = await startService(t)
  const r = await fetch(`http://127.0.0.1:${port}/health`)
  assert.equal(r.status, 401)
})

test('turn-finished with prompt pairing drains spool to aiduMEI /add', async t => {
  const { port, spoolDir, received, scratch } = await startService(t)
  // Simulate the plugin recording a prompt.
  const sessionId = 'ses-drain-test'
  // With HOME=scratch, the service reads stateDir = scratch/.aidumei-openchamber.
  const promptsDir = path.join(scratch, '.aidumei-openchamber', 'prompts')
  await mkdir(promptsDir, { recursive: true })
  await writeFile(path.join(promptsDir, `${sessionId}.json`), JSON.stringify({ sessionId, text: '蓝天橙猫 是什么', at: Date.now() }))
  const r = await postEvent(port, {
    event: { id: 'evt-drain-1', type: 'turn-finished', at: Date.now(), session: { id: sessionId, parentId: null }, project: { directory: '/tmp' } },
    content: { type: 'turn-finished', sessionTitle: 'test', reply: '蓝天橙猫 是答案。', replyTruncated: false },
  })
  assert.equal(r.status, 200)
  // Wait for drain — poll until /add lands or 10s budget expires.
  let adds = []
  for (let i = 0; i < 20; i++) {
    await new Promise(resolve => setTimeout(resolve, 500))
    adds = received.filter(x => x.url.startsWith('/add'))
    if (adds.length >= 1) break
  }
  assert.ok(adds.length >= 1, `service should forward to /add (waited 10s; received: ${received.map(x => x.url).join(',')})`)
  const body = JSON.parse(adds[0].body)
  assert.equal(body.user_id, 'och-test')
  assert.ok(body.messages.length >= 2, 'should have user + assistant messages')
  assert.equal(body.messages[0].role, 'user')
})

test('turn-failed does not forward to /add', async t => {
  const { port, received } = await startService(t)
  await postEvent(port, {
    event: { id: 'evt-fail-1', type: 'turn-failed', at: Date.now(), session: { id: 'ses-fail', parentId: null }, project: { directory: '/tmp' } },
    content: { type: 'turn-failed', sessionTitle: null, error: { type: 'test', message: 'boom' } },
  })
  await new Promise(resolve => setTimeout(resolve, 800))
  const adds = received.filter(x => x.url.startsWith('/add'))
  assert.equal(adds.length, 0, 'turn-failed should not trigger /add')
})

test('service handles malformed JSON without crashing', async t => {
  const { port } = await startService(t)
  const r = await fetch(`http://127.0.0.1:${port}/session-events`, {
    method: 'POST',
    headers: { Authorization: 'Bearer svc-tok', 'Content-Type': 'application/json' },
    body: 'not-json',
  })
  assert.equal(r.status, 400)
  // Service still alive.
  const health = await fetch(`http://127.0.0.1:${port}/health`, { headers: { Authorization: 'Bearer svc-tok' } })
  assert.equal(health.status, 200)
})

// helper re-export for the prompt-write test
import { writeFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
