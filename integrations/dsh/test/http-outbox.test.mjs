import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, readdir, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { __test, resolveOptions } from '../index.mjs'

function json(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(value))
}

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  assert.fail('condition was not reached before timeout')
}

test('outbox writes committed turns and distill summaries with stable receipts', async t => {
  const spoolDir = await mkdtemp(path.join(os.tmpdir(), 'aiduMEI-dsh-outbox-'))
  const calls = []
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    const body = raw ? JSON.parse(raw) : {}
    calls.push({ method: req.method, path: req.url, headers: req.headers, body })
    if (req.url.startsWith('/session/distill')) {
      return json(res, 200, {
        status: 'ok',
        summary: 'distilled preview summary',
        metadata: { source: 'session-distill' },
        source_fingerprint: 'fingerprint-1',
      })
    }
    return json(res, 200, { status: 'ok', action: 'direct' })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(async () => {
    await new Promise(resolve => server.close(resolve))
    await rm(spoolDir, { recursive: true, force: true })
  })

  const { port } = server.address()
  const options = resolveOptions({
    apiBase: `http://127.0.0.1:${port}`,
    userId: 'dsh-test',
    bankId: 'dsh-preview',
    spoolDir,
    allowUnscoped: true,
  })
  const client = new __test.MemoryClient(options, { warn() {} })
  const outbox = new __test.Outbox(options, client, { warn() {} })
  t.after(() => outbox.close())
  await outbox.init()

  await outbox.enqueueTurn({
    sessionId: 'dsh-session-1',
    rawSessionId: 'session-1',
    turn: 2,
    endSeq: 9,
    reason: 'completed',
    messages: [{ role: 'user', content: 'user text' }, { role: 'assistant', content: 'answer' }],
  })
  await waitFor(async () => calls.filter(call => call.path === '/add').length === 1
    && !(await readdir(options.spoolDir)).some(name => name.startsWith('turn-')))

  await outbox.enqueueDistill('dsh-session-1', true)
  await waitFor(async () => calls.filter(call => call.path === '/add').length === 2
    && !(await readdir(options.spoolDir)).some(name => /^(summary|distill)-/.test(name)))

  const addCalls = calls.filter(call => call.path === '/add')
  assert.equal(addCalls.length, 2)
  assert.match(addCalls[0].headers['idempotency-key'], /^dsh-ingest-/)
  assert.equal(addCalls[0].body.metadata.source, 'dsh-ingest')
  assert.equal(addCalls[0].body.metadata._origin_session_id, 'dsh-session-1')
  assert.match(addCalls[1].headers['idempotency-key'], /^dsh-distill-/)
  assert.equal(addCalls[1].body.messages, 'distilled preview summary')
  assert.equal(addCalls[1].body.metadata.session_id, 'distill:dsh-session-1')
})
