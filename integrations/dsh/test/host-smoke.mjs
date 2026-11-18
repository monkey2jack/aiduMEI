// Native DSH lifecycle test. The model is a deterministic Messages fixture;
// memory uses an isolated real API when AIDUMEI_TEST_URL is supplied.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { __test, resolveOptions } from '../index.mjs'
import { dshCommand } from '../cli.mjs'

const cli = process.env.DSH_CLI_CMD
if (!cli && !process.env.DSH_CLI_ENTRY) throw new Error('DSH_CLI_ENTRY or DSH_CLI_CMD must name the installed DSH CLI')
const root = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-host-'))
const workspace = path.join(root, 'workspace')
await mkdir(workspace)
const packageDir = fileURLToPath(new URL('..', import.meta.url))
const userId = `dsh-ci-${process.pid}`
const bankId = 'dsh-preview'
const sessions = Number(process.env.DSH_SMOKE_SESSIONS || 1)
const turns = Number(process.env.DSH_SMOKE_TURNS || 3)
const faultMode = process.env.DSH_SMOKE_FAULTS === '1'
if (faultMode && sessions < 5) throw new Error('fault testing requires five sessions')
const requests = []
const adds = []
const keys = new Set()
let modelCalls = 0
let toolCalls = 0
let mcpDiscovered = false
let mcpRecallVerified = false
let recallSeen = false
let offline = false
let fault
const faultEvidence = []
const reply = (res, body) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)) }
const memory = createServer(async (req, res) => {
  let raw = ''
  for await (const part of req) raw += part
  const body = raw ? JSON.parse(raw) : {}
  requests.push(req.url)
  if (offline) { res.writeHead(503); res.end('{"error":"synthetic_network_outage"}'); return }
  if (process.env.AIDUMEI_TEST_URL) {
    const upstream = await fetch(`${process.env.AIDUMEI_TEST_URL}${req.url}`, { method: req.method,
      headers: { 'Content-Type': 'application/json', ...(req.headers.authorization ? { Authorization: req.headers.authorization } : {}) },
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : raw, redirect: 'error', signal: AbortSignal.timeout(35000) })
    const data = await upstream.text()
    if (fault?.kind === 'lost-ack' && req.url.startsWith('/add') && body.metadata?.source === 'dsh-ingest') {
      fault.trigger(); await fault.gate
    }
    res.writeHead(upstream.status, { 'Content-Type': 'application/json' }); res.end(data)
    return
  }
  if (req.url.startsWith('/api/core-memory')) return reply(res, { blocks: { persona: { content: 'Confirmed smoke-test core memory.' } } })
  if (req.url.startsWith('/search')) return reply(res, { results: [{ memory: 'Smoke-test memory: the preview color is violet.' }] })
  if (req.url.startsWith('/session/distill')) return reply(res, { status: 'skipped', reason: 'too_short' })
  if (req.url.startsWith('/add')) {
    const key = req.headers['idempotency-key'] || body.idempotency_key
    if (key && !keys.has(key)) { keys.add(key); adds.push(body) }
    if (fault?.kind === 'lost-ack' && body.metadata?.source === 'dsh-ingest') { fault.trigger(); await fault.gate }
    return reply(res, { status: 'ok' })
  }
  return reply(res, { status: 'ok', health_status: 'ok', version: 'fixture' })
})
await new Promise(resolve => memory.listen(0, '127.0.0.1', resolve))
const apiBase = `http://127.0.0.1:${memory.address().port}`
const model = createServer(async (req, res) => {
  try {
    let raw = ''
    for await (const part of req) raw += part
    const body = JSON.parse(raw)
    modelCalls++
    if (fault?.kind === 'cancel') { fault.trigger(); await fault.gate }
    const text = JSON.stringify(body.messages)
    recallSeen ||= /\[DATA:[a-f0-9]{32} aiduMEI/.test(text)
    const mcpName = body.tools?.find(tool => tool.name === 'mcp__aidumei__mem_search')?.name
    mcpDiscovered ||= Boolean(mcpName)
    const last = body.messages?.at(-1)
    const toolResults = last?.content?.filter(block => block.type === 'tool_result') || []
    const toolResult = toolResults.length > 0
    if (toolResult) {
      assert.ok(toolResults.every(block => !block.is_error), 'MCP must not return a transport/tool error')
      const resultText = JSON.stringify(toolResults)
      assert.match(resultText, /violet/, 'MCP search must return the seeded preview color')
      assert.doesNotMatch(resultText, /unauthorized|forbidden|invalid_token|connection refused/i)
      mcpRecallVerified = true
    }
    const useTool = Boolean(mcpName && text.includes('DSH_TOOL_LOOP') && !toolResult)
    // Exercise the launcher's configured defaults, as normal host tool calls do.
    const block = useTool ? { type: 'tool_use', id: `tool-${modelCalls}`, name: mcpName, input: { query: 'preview color' } }
      : { type: 'text', text: 'DSH native harness smoke answer.' }
    if (useTool) toolCalls++
    const events = [
      { type: 'message_start', message: { id: `msg-${modelCalls}`, type: 'message', role: 'assistant', model: body.model, usage: { input_tokens: 20, output_tokens: 1 } } },
      { type: 'content_block_start', index: 0, content_block: block },
      { type: 'content_block_stop', index: 0 },
      { type: 'message_delta', delta: { stop_reason: useTool ? 'tool_use' : 'end_turn' }, usage: { output_tokens: 10 } },
      { type: 'message_stop' },
    ]
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.end(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''))
  } catch (error) {
    await writeFile(path.join(root, 'model-fixture-error.log'), String(error), { mode: 0o600 })
    res.writeHead(500); res.end('invalid_fixture_request')
  }
})
await new Promise(resolve => model.listen(0, '127.0.0.1', resolve))
const env = { ...process.env, DSH_HOME: path.join(root, 'home'), DEEPSEEK_API_KEY: 'fixture-not-a-secret',
  DEEPSEEK_BASE_URL: `http://127.0.0.1:${model.address().port}/anthropic`, AIDUMEI_TEST_URL: apiBase,
  AIDUMEI_DSH_SPOOL_DIR: path.join(root, 'spool') }
if (!env.AIDUMEI_TEST_TOKEN_FILE) {
  env.AIDUMEI_TEST_TOKEN_FILE = path.join(root, 'test.token')
  await writeFile(env.AIDUMEI_TEST_TOKEN_FILE, 'fixture-not-a-secret', { mode: 0o600 })
}

async function run(args, label, interruption) {
  const invocation = await dshCommand({ cli, env })
  const child = spawn(invocation.command, [...invocation.args, ...args], { cwd: workspace, env: invocation.env, stdio: ['ignore', 'pipe', 'pipe'] })
  let stdout = '', stderr = ''
  child.stdout.on('data', data => { stdout += data })
  child.stderr.on('data', data => { stderr += data })
  let interrupted = false
  const timer = setTimeout(() => child.kill(), 90000)
  if (interruption) interruption.triggered.then(() => {
    interrupted = true
    child.kill(interruption.kind === 'cancel' ? 'SIGINT' : 'SIGKILL')
  })
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve) })
  clearTimeout(timer)
  await writeFile(path.join(root, `${label}.stdout`), stdout, { mode: 0o600 })
  await writeFile(path.join(root, `${label}.stderr`), stderr, { mode: 0o600 })
  if (interruption) {
    interruption.release()
    assert.ok(interrupted, 'the requested interruption reached the real host')
    return stdout
  }
  if (code !== 0) throw new Error(`DSH ${label} exit=${code}; evidence=${root}`)
  return stdout
}

function interrupt(kind) {
  let trigger, release
  const triggered = new Promise(resolve => { trigger = resolve })
  const gate = new Promise(resolve => { release = resolve })
  return { kind, trigger, triggered, gate, release }
}

async function manage(action) {
  const args = [path.join(packageDir, 'manage.mjs'), action, '--profile', 'aidumei-test', '--dsh-home', env.DSH_HOME]
  if (cli) args.push('--dsh-cli', cli)
  if (action === 'install') {
    args.push('--api', apiBase, '--user', userId, '--bank', bankId, '--workdir', workspace, '--token-file', env.AIDUMEI_TEST_TOKEN_FILE)
    if (process.env.DSH_MCP_PYTHON) args.push('--python', process.env.DSH_MCP_PYTHON)
  }
  const child = spawn(process.execPath, args, { env, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  child.stdout.on('data', data => { output += data })
  child.stderr.on('data', data => { output += data })
  const timer = setTimeout(() => child.kill(), 240000)
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve) }).finally(() => clearTimeout(timer))
  await writeFile(path.join(root, `manage-${action.replace('--', '')}.log`), output, { mode: 0o600 })
  assert.equal(code, 0, `manage ${action} must pass; evidence=${root}`)
}

try {
  if (process.env.AIDUMEI_TEST_URL) {
    const client = new __test.MemoryClient(resolveOptions({ apiBase, userId, bankId }, env), console)
    const seeded = await client.add([{ role: 'user', content: 'The preview color is violet. This is the confirmed color for the memory bridge demonstration.' }],
      { source: 'dsh-preview-fixture' }, `dsh-preview-seed-${userId}`)
    assert.equal(seeded.status, 'ok', 'test memory is seeded in the isolated scope')
    const found = await client.search('preview color')
    assert.match(JSON.stringify(found), /violet/, 'seed memory is readable before host testing')
  }
  await run(['--profile', 'aidumei-test', '--from-default-profile', 'headless', '--dump-config'], 'init')
  await manage('install')
  await manage('--check')
  const config = { apiBase, userId, bankId, instanceId: 'aidumei-test', allowedRoots: [workspace], spoolDir: path.join(root, 'spool'), idleDistillMs: 0 }
  const patch = [
    { id: 'aidumei-dsh', config },
    { id: 'session-title-llm', disabled: true },
    { id: 'llm-deepseek', config: { baseURL: env.DEEPSEEK_BASE_URL, thinking: 'disabled' } },
  ]
  const patchFile = path.join(root, 'test.patch.json')
  await writeFile(patchFile, JSON.stringify(patch), { mode: 0o600 })
  const ids = []
  for (let s = 0; s < sessions; s++) {
    let sessionId
    for (let t = 0; t < turns; t++) {
      const args = ['--profile', 'aidumei-test', '--patch', patchFile, '--json', '--approval-policy', 'never']
      if (sessionId) args.push('--session-id', sessionId)
      if (faultMode && s === 1 && t === 0) offline = true
      if (faultMode && [2, 3].includes(s) && t === 1) {
        fault = interrupt(s === 2 ? 'cancel' : 'lost-ack')
        const interrupted = await run([...args, 'Synthetic interrupted preview question.'], `session-${s}-interrupted`, fault)
        assert.ok(interrupted.split('\n').filter(Boolean).map(line => JSON.parse(line)).some(event => event.type === 'session'))
        if (fault.kind === 'lost-ack') {
          const state = resolveOptions(config, env)
          assert.ok((await readdir(state.spoolDir)).some(name => name.startsWith('turn-') && name.endsWith('.json')),
            'a killed process retains its uncertain write for replay')
        }
        faultEvidence.push({ kind: fault.kind, processInterrupted: true })
        fault = undefined
      }
      const output = await run([...args, `Synthetic DSH preview session ${s} round ${t}. ${t === 1 ? 'DSH_TOOL_LOOP' : 'Answer briefly.'}`], `session-${s}-turn-${t}`)
      const events = output.split('\n').filter(Boolean).map(line => JSON.parse(line))
      sessionId = events.find(event => event.type === 'session')?.sessionId
      assert.ok(sessionId, 'native session identity is present')
      assert.equal(events.findLast(event => event.type === 'final')?.reason?.kind, 'completed')
      if (offline) {
        const state = resolveOptions(config, env)
        assert.ok((await readdir(state.spoolDir)).some(name => name.startsWith('turn-') && name.endsWith('.json')))
        offline = false
        faultEvidence.push({ kind: 'network-outage', completedWithoutMemory: true, pendingPersisted: true })
      }
    }
    ids.push(sessionId)
  }
  assert.ok(recallSeen, 'the actual model request contains nonce-fenced recall')
  if (process.env.DSH_MCP_PYTHON) {
    assert.ok(mcpDiscovered, 'the native DSH tool catalog includes aiduMEI MCP')
    assert.ok(toolCalls > 0, 'the native DSH agent completed an MCP tool loop')
    assert.ok(mcpRecallVerified, 'the MCP response contains the seeded memory')
  }
  if (!process.env.AIDUMEI_TEST_URL) {
    const ingests = adds.filter(body => body.metadata?.source === 'dsh-ingest')
    assert.ok(ingests.length >= sessions * turns)
    assert.ok(ingests.filter(body => body.metadata.dsh_end_reason === 'completed').every(body => body.messages.length === 2 && body.messages[0].role === 'user' && body.messages[1].role === 'assistant'))
    assert.ok(requests.some(url => url.startsWith('/session/distill')))
  }
  const options = resolveOptions(config, env)
  const queue = (await readdir(options.spoolDir)).filter(name => /^(turn|summary)-/.test(name))
  assert.deepEqual(queue, [], 'all native completed turns were acknowledged')
  const receipts = (await readdir(path.join(options.spoolDir, 'receipts'))).length
  assert.ok(receipts >= sessions * turns)
  await manage('uninstall')
  const manifest = JSON.parse(await readFile(path.join(env.DSH_HOME, 'profiles', 'aidumei-test', 'package.json'), 'utf8'))
  assert.ok(!manifest.dsh?.profile?.bundles?.includes('@monkey2jack/aidumei-dsh'))
  await run(['--profile', 'aidumei-test', '--dump-config'], 'after-uninstall')
  const report = { status: 'PASS', nativeDsh: '0.2.2-rc.3', model: 'deterministic Messages fixture',
    api: process.env.AIDUMEI_TEST_URL ? 'isolated real API' : 'loopback HTTP fixture', sessions, turnsPerSession: turns,
    modelCalls, toolCalls, mcpDiscovered, mcpRecallVerified, recallSeen, receipts, faults: faultEvidence,
    installCheckUninstall: true, sessionIds: ids, evidence: root }
  await writeFile(path.join(root, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
  console.log(JSON.stringify(report))
} finally {
  model.closeAllConnections(); memory.closeAllConnections()
  await Promise.all([new Promise(resolve => model.close(resolve)), new Promise(resolve => memory.close(resolve))])
}
