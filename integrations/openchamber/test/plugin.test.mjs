import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const pluginUrl = process.env.AIDUMEI_PLUGIN_UNDER_TEST
  ? pathToFileURL(process.env.AIDUMEI_PLUGIN_UNDER_TEST).href : new URL('../plugin/index.js', import.meta.url).href

async function fixture(t, { allow = true, testMode = false, fail = false } = {}) {
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'aidumei-och-plugin-'))
  const oldEnv = { ...process.env }
  const oldFetch = globalThis.fetch
  for (const key of Object.keys(process.env)) if (/^AIDUMEM|^AIDUMEI/.test(key)) delete process.env[key]
  process.env.HOME = scratch
  process.env.USERPROFILE = scratch
  const workspace = path.join(scratch, 'workspace')
  await mkdir(workspace)
  const root = path.join(scratch, '.aidumei-openchamber')
  const state = testMode ? path.join(root, 'test') : root
  await mkdir(state, { recursive: true })
  const tokenFile = path.join(scratch, 'token')
  await writeFile(tokenFile, 'file-token')
  const config = { apiBase: 'http://127.0.0.1:1', apiTokenFile: tokenFile, userId: 'test-user', bankId: 'test-bank', testMode, allowedRoots: allow ? [workspace] : [] }
  await writeFile(path.join(state, 'config.json'), JSON.stringify(config))
  if (testMode) {
    process.env.AIDUMEI_TEST_URL = config.apiBase
    process.env.AIDUMEM_URL = 'http://production.invalid'
    process.env.AIDUMEM_API_TOKEN = 'production-secret'
    await writeFile(path.join(root, 'config.json'), JSON.stringify({ apiBase: 'http://production.invalid', apiToken: 'production-secret' }))
  }
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), ...options, body: options.body ? JSON.parse(options.body) : null })
    if (fail) throw new Error('unavailable')
    return new Response(JSON.stringify(String(url).endsWith('/search')
      ? { recall_verdict: 'found', results: [{ memory: 'A stored fact.' }] }
      : { blocks: {} }), { status: 200 })
  }
  const hooks = {}
  const ctx = { session: {
    hook: async (name, callback) => { hooks[name] = callback },
    get: async ({ path: { id } }) => ({ id, location: { directory: workspace } }),
  } }
  const mod = await import(`${pluginUrl}?fixture=${encodeURIComponent(scratch)}`)
  const dispose = await mod.AIDUMEI_PLUGIN.setup(ctx)
  t.after(async () => {
    dispose?.()
    globalThis.fetch = oldFetch
    for (const key of Object.keys(process.env)) if (!(key in oldEnv)) delete process.env[key]
    Object.assign(process.env, oldEnv)
    await rm(scratch, { recursive: true, force: true })
  })
  const prompt = (id = 'message-1', text = 'A question') => hooks.prompt({ sessionID: 'session-1', messageID: id, prompt: { text } })
  const event = (text = 'A question') => ({ sessionID: 'session-1', messages: [{ role: 'user', content: [{ type: 'text', text }] }], system: [] })
  return { scratch, workspace, root, state, tokenFile, hooks, calls, ctx, prompt, event }
}

test('default-empty allowlist performs no recall and retains no prompt text', async t => {
  const f = await fixture(t, { allow: false })
  await f.prompt()
  await f.hooks.context(f.event())
  assert.equal(f.calls.length, 0)
  assert.deepEqual(await readdir(path.join(f.state, 'prompts')), [])
})

test('a symlink outside an allowed workspace cannot opt that workspace into recall', async t => {
  const f = await fixture(t)
  const outside = path.join(f.scratch, 'outside')
  await mkdir(outside)
  const link = path.join(f.workspace, 'escape')
  await symlink(outside, link, process.platform === 'win32' ? 'junction' : 'dir')
  f.ctx.session.get = async ({ path: { id } }) => ({ id, location: { directory: link } })
  await f.prompt()
  assert.equal(f.calls.length, 0)
})

test('prefetch admission reaches the final model request even when context callbacks are not awaited', async t => {
  const f = await fixture(t)
  // OpenCode v2.3.0 waits for prompt hooks. A stricter context scheduler copies
  // the request synchronously and deliberately ignores the hook return value.
  await f.prompt()
  const event = f.event()
  f.hooks.context(event)
  const modelRequest = structuredClone({ messages: event.messages, system: event.system })
  assert.equal(modelRequest.system.length, 1)
  assert.match(modelRequest.system[0].text, /\[DATA:[a-f0-9]+ aiduMEI/)
  assert.match(modelRequest.system[0].text, /A stored fact/)
  const search = f.calls.find(call => call.body?.query)
  assert.equal(search.body.session_id, 'och-session-1')
  assert.equal(search.headers.Authorization, 'Bearer file-token')
  f.hooks.context(event)
  assert.equal(event.system.length, 1, 'a tool loop cannot inject the same turn twice')
})

test('identical questions in distinct message IDs each inject and each retain a separate prompt', async t => {
  const f = await fixture(t)
  await f.prompt('message-1')
  const first = f.event()
  f.hooks.context(first)
  await f.prompt('message-2')
  const second = f.event()
  f.hooks.context(second)
  assert.equal(first.system.length, 1)
  assert.equal(second.system.length, 1)
  assert.equal((await readdir(path.join(f.state, 'prompts'))).length, 2)
})

test('test realm uses its own config and prompts without touching production state', async t => {
  const f = await fixture(t, { testMode: true })
  const before = await readFile(path.join(f.root, 'config.json'), 'utf8')
  await f.prompt()
  assert.equal(await readFile(path.join(f.root, 'config.json'), 'utf8'), before)
  assert.equal((await readdir(path.join(f.state, 'prompts'))).length, 1)
  assert.equal(await readdir(path.join(f.root, 'prompts')).catch(() => []).then(names => names.length), 0)
  assert.ok(f.calls.every(call => !call.url.includes('production.invalid')))
  assert.ok(f.calls.every(call => call.headers.Authorization === 'Bearer file-token'))
})

test('token rotation is observed by the next turn and no body is printed for an invalid system field', async t => {
  const f = await fixture(t)
  await f.prompt('message-1')
  await writeFile(f.tokenFile, 'rotated-token')
  await f.prompt('message-2')
  assert.equal(f.calls.at(-1).headers.Authorization, 'Bearer rotated-token')
  const originalError = console.error
  const logs = []
  console.error = (...args) => logs.push(args.join(' '))
  try { f.hooks.context({ ...f.event(), system: 'private-system-content' }) } finally { console.error = originalError }
  assert.match(logs.join(' '), /system_not_array/)
  assert.doesNotMatch(logs.join(' '), /private-system-content/)
})

test('three failed turns open the breaker without being reset by another prompt', async t => {
  const f = await fixture(t, { fail: true })
  for (let n = 0; n < 3; n += 1) await f.prompt(`message-${n}`)
  assert.equal(f.calls.length, 6)
  await f.prompt('message-next')
  assert.equal(f.calls.length, 6)
})

test('assistant continuation and hooks-off never inject', async t => {
  const f = await fixture(t)
  await f.prompt()
  const event = f.event()
  event.messages.push({ role: 'assistant', content: 'continuation' })
  await f.hooks.context(event)
  assert.equal(event.system.length, 0)
  process.env.AIDUMEI_HOOKS = 'off'
  const hooks = {}
  const mod = await import(pluginUrl)
  await mod.AIDUMEI_PLUGIN.setup({ session: { hook: (name, callback) => { hooks[name] = callback } } })
  assert.deepEqual(hooks, {})
})
