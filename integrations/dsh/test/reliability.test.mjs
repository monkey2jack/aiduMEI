import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { __test, apply, isAllowedWorkspace, resolveOptions } from '../index.mjs'

const silent = { warn() {} }
const turn = (n = 1) => ({ sessionId: 'dsh-recovery', turn: n, endSeq: n * 5, reason: 'completed',
  messages: [{ role: 'user', content: `question ${n}` }, { role: 'assistant', content: `answer ${n}` }] })
const pending = async options => (await readdir(options.spoolDir)).filter(name => /^(turn|summary|distill)-/.test(name))
async function until(predicate, timeout = 3000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  assert.fail('timed out waiting for observable state')
}
async function fixture(t, config = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-reliable-'))
  const options = resolveOptions({ apiBase: 'http://127.0.0.1:1', allowUnscoped: true, spoolDir: root, ...config }, {})
  const boxes = []
  t.after(async () => {
    for (const box of boxes) await box.close()
    await rm(root, { recursive: true, force: true })
  })
  return { root, options, box: async client => {
    const box = new __test.Outbox(options, client, silent)
    boxes.push(box)
    await box.init()
    return box
  } }
}

test('realpath rejects symlink and Windows junction escapes and options preserve native roots', async t => {
  const { root } = await fixture(t)
  const allowed = path.join(root, 'allowed')
  const outside = path.join(root, 'outside')
  await mkdir(allowed)
  await mkdir(outside)
  await symlink(outside, path.join(allowed, 'escape'), process.platform === 'win32' ? 'junction' : 'dir')
  const options = resolveOptions({ allowedRoots: [allowed] }, {})
  assert.equal(options.allowedRoots[0], allowed)
  assert.equal(await isAllowedWorkspace(allowed, options), true)
  assert.equal(await isAllowedWorkspace(path.join(allowed, 'escape'), options), false)
  assert.equal(await isAllowedWorkspace(outside, options), false)
})

test('network failure survives restart, preserves ordering and acknowledged turns never replay', async t => {
  const f = await fixture(t)
  let attempts = 0
  const failed = await f.box({ add: async () => { attempts++; throw new Error('offline') } })
  await Promise.all([3, 1, 2].map(n => failed.enqueueTurn(turn(n))))
  await until(() => attempts > 0 && !failed.running)
  assert.equal((await pending(f.options)).filter(name => name.startsWith('turn-')).length, 3)
  await failed.close()
  const delivered = []
  const recovered = await f.box({ add: async (messages, metadata) => { delivered.push(metadata._origin_turn); return { status: 'ok' } } })
  await until(async () => !(await pending(f.options)).some(name => name.startsWith('turn-')))
  assert.deepEqual(delivered, [1, 2, 3])
  assert.ok((await pending(f.options)).some(name => name.startsWith('distill-')), 'idle job is durable')
  for (const n of [1, 2, 3]) await recovered.enqueueTurn(turn(n))
  await recovered.mutations
  assert.deepEqual(delivered, [1, 2, 3])
})

test('a live spool has one owner and permanent 409 repair errors stay held across restart', async t => {
  const f = await fixture(t)
  let calls = 0
  const box = await f.box({ add: async () => { calls++; throw new __test.HttpError(409, { detail: { status: 'repair_required' } }) } })
  const contender = new __test.Outbox(f.options, {}, silent)
  await assert.rejects(contender.init(), /outbox_in_use/)
  await box.enqueueTurn(turn())
  await until(async () => (await pending(f.options)).some(name => name.endsWith('.held')))
  await box.close()
  const recovered = await f.box({ add: async () => { calls++; return { status: 'ok' } }, distill: async () => assert.fail('held turn must block distill') })
  await recovered.enqueueTurn(turn())
  await recovered.enqueueDistill('dsh-recovery', true)
  await until(() => !recovered.running)
  assert.equal(calls, 1)
})

test('invalid successful acknowledgements are held instead of silently dropping the turn', async t => {
  const f = await fixture(t)
  const box = await f.box({ add: async () => ({ status: 'queued' }) })
  await box.enqueueTurn(turn())
  await until(async () => (await pending(f.options)).some(name => name.startsWith('turn-') && name.endsWith('.held')))
  assert.equal((await readdir(box.receiptDir)).length, 0)
})

test('concurrent restarts reclaim one dead owner without deleting the winning lease', async t => {
  const f = await fixture(t)
  const exited = spawnSync(process.execPath, ['-e', 'process.exit(0)'])
  assert.equal(exited.status, 0)
  const lock = path.join(f.options.spoolDir, '.owner')
  await mkdir(lock, { recursive: true })
  await writeFile(path.join(lock, 'pid.json'), JSON.stringify({ pid: exited.pid, lease: 'dead-owner' }))
  const results = await Promise.allSettled(Array.from({ length: 8 }, () => f.box({})))
  const winners = results.filter(result => result.status === 'fulfilled')
  assert.equal(winners.length, 1)
  const owner = JSON.parse(await readFile(path.join(lock, 'pid.json'), 'utf8'))
  assert.equal(owner.lease, winners[0].value.lease)
  await winners[0].value.close()
  await winners[0].value.close()
  assert.ok(!(await readdir(f.options.spoolDir)).includes('.owner'))
})

test('a failed summary write retains its exact payload when a newer turn replaces the schedule', async t => {
  const f = await fixture(t)
  let summaryAttempted = false
  const first = await f.box({
    add: async messages => {
      if (typeof messages === 'string') { summaryAttempted = true; throw new Error('offline') }
      return { status: 'ok' }
    },
    distill: async () => ({ status: 'ok', summary: 'frozen summary 1', source_fingerprint: 'first' }),
  })
  await first.enqueueTurn(turn())
  await first.enqueueDistill('dsh-recovery', true)
  await until(() => summaryAttempted && !first.running)
  await first.enqueueTurn(turn(2))
  await first.close()
  const delivered = []
  await f.box({ add: async messages => { delivered.push(messages); return { status: 'ok' } },
    distill: async () => assert.fail('new idle job is not due yet') })
  await until(async () => !(await pending(f.options)).some(name => /^(turn|summary)-/.test(name)))
  assert.ok(delivered.some(value => value === 'frozen summary 1'))
  assert.ok(delivered.some(value => Array.isArray(value) && value[0].content === 'question 2'))
})

test('global off registers no hooks, creates no spool and performs no recovery', async t => {
  const f = await fixture(t)
  const old = process.env.AIDUMEI_HOOKS
  process.env.AIDUMEI_HOOKS = 'off'
  try {
    await apply({ on() { assert.fail('off must not register listeners') }, logger: silent }, { spoolDir: path.join(f.root, 'disabled'), apiBase: 'invalid' })
  } finally {
    if (old === undefined) delete process.env.AIDUMEI_HOOKS
    else process.env.AIDUMEI_HOOKS = old
  }
  assert.deepEqual(await readdir(f.root), [])
})

test('host flush waits for local persistence while HTTP is stalled; reload scans receipts without duplicate writes', async t => {
  const f = await fixture(t)
  let release
  const gate = new Promise(resolve => { release = resolve })
  let adds = 0
  const server = createServer(async (req, res) => {
    for await (const _ of req) { /* drain request */ }
    if (req.url === '/add') { adds++; await gate }
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(req.url.startsWith('/api/core-memory') ? { blocks: { persona: { content: 'confirmed core' } } }
      : req.url === '/search' ? { results: [{ memory: 'historical fact' }] } : { status: 'ok' }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(async () => { release(); await new Promise(resolve => server.close(resolve)) })
  const config = { apiBase: `http://127.0.0.1:${server.address().port}`, spoolDir: f.root, allowedRoots: [f.root] }
  const options = resolveOptions(config)
  const handlers = new Map()
  const ctx = { logger: silent, on(event, cb) { handlers.set(event, cb) } }
  await apply(ctx, config)
  t.after(async () => { release(); await handlers.get('dispose')?.() })
  const user = { id: 'u1', role: 'user', content: [{ type: 'text', text: 'real question' }], source: { kind: 'user' } }
  const events = [
    { type: 'turn/start', seq: 0, data: { turn: 1 } },
    { type: 'user/message', seq: 1, data: user },
    { type: 'assistant/message', seq: 2, data: { turn: 1, message: { content: [{ type: 'text', text: 'answer' }] } } },
    { type: 'turn/end', seq: 3, data: { turn: 1 } },
  ]
  const session = { id: 'host-session', header: { cwd: f.root }, snapshotEvents: () => events }
  const decision = { kind: 'enter', messages: [user], tools: ['preserve-me'], custom: true }
  const recalled = await handlers.get('agent/pre-step')({ agent: { session }, turn: 2, step: 1 }, async () => decision)
  assert.equal(recalled.custom, true)
  assert.deepEqual(recalled.tools, decision.tools)
  assert.match(recalled.messages.at(-1).content[0].text, /confirmed core/)
  handlers.get('session/event')(session, events.at(-1))
  await handlers.get('session/flush')(session)
  assert.ok((await pending(options)).some(name => name.startsWith('turn-')))
  await until(() => adds === 1)
  release()
  await until(async () => !(await pending(options)).some(name => name.startsWith('turn-')))
  await handlers.get('dispose')()
  handlers.clear()
  await apply(ctx, config)
  handlers.get('session/created')(session)
  await handlers.get('session/flush')(session)
  assert.equal(adds, 1)
  assert.ok(!(await pending(options)).some(name => name.startsWith('turn-')))
})
