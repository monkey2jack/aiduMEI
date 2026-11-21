import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// setup() writes ~/.aidumei-openchamber; never let a test touch the developer's real home.
process.env.HOME = mkdtempSync(join(tmpdir(), 'aidumei-och-plugin-home-'))
process.env.USERPROFILE = process.env.HOME

// The plugin is loaded by OpenCode with a bare import() and no node_modules.
// We import it directly (it has no dependencies) and drive setup() with a
// fake ctx that records hook registrations and fires the context hook.

function createFakeCtx() {
  const hooks = {}
  const ctx = {
    session: {
      hook: async (name, handler) => {
        hooks[name] = handler
      },
    },
  }
  return { ctx, hooks }
}

function fakeEnv(overrides = {}) {
  return { ...overrides }
}

// The plugin reads process.env at setup time. We can't easily swap process.env
// per test in a parallel test runner, so instead we drive the exported plugin
// module and verify its pure helpers + the hook behavior end-to-end with the
// real env (which the test runner controls via the parent process).

test('plugin module loads with zero dependencies and exports id + setup', async () => {
  const mod = await import('../plugin/index.js')
  assert.equal(typeof mod.AIDUMEI_PLUGIN.id, 'string')
  assert.equal(mod.AIDUMEI_PLUGIN.id, 'aidumei-openchamber')
  assert.equal(typeof mod.AIDUMEI_PLUGIN.setup, 'function')
})

test('plugin setup registers the context hook (AIDUMEI_HOOKS not off)', async () => {
  const mod = await import('../plugin/index.js')
  const { ctx, hooks } = createFakeCtx()
  await mod.AIDUMEI_PLUGIN.setup(ctx)
  assert.equal(typeof hooks['context'], 'function')
})

test('context hook injects recall fence into messages on first call', async t => {
  // This test requires a live aiduMEI API; skip if unreachable.
  const apiBase = process.env.AIDUMEI_TEST_URL || ''
  if (!apiBase) return t.skip('AIDUMEI_TEST_URL not set')
  const mod = await import('../plugin/index.js')
  const { ctx, hooks } = createFakeCtx()
  await mod.AIDUMEI_PLUGIN.setup(ctx)
  const messages = [
    { role: 'user', content: [{ type: 'text', text: '测试暗号蓝天橙猫是什么' }] },
  ]
  await hooks['context']({ messages, sessionID: 'test-ses-1' })
  const injected = messages.find(m => m.metadata?.source === 'aidumei-openchamber')
  assert.ok(injected, 'recall fence should be injected into messages')
  assert.match(injected.content[0].text, /\[DATA:[a-f0-9]+ aiduMEI/)
  assert.match(injected.content[0].text, /\[END OF DATA CONTEXT/)
})

test('context hook turn guard: second call with same tail does not re-inject', async t => {
  const apiBase = process.env.AIDUMEI_TEST_URL || ''
  if (!apiBase) return t.skip('AIDUMEI_TEST_URL not set')
  const mod = await import('../plugin/index.js')
  const { ctx, hooks } = createFakeCtx()
  await mod.AIDUMEI_PLUGIN.setup(ctx)
  const messages = [
    { role: 'user', content: [{ type: 'text', text: '测试暗号蓝天橙猫是什么' }] },
  ]
  await hooks['context']({ messages, sessionID: 'test-ses-guard' })
  const count1 = messages.filter(m => m.metadata?.source === 'aidumei-openchamber').length
  await hooks['context']({ messages, sessionID: 'test-ses-guard' })
  const count2 = messages.filter(m => m.metadata?.source === 'aidumei-openchamber').length
  assert.equal(count1, count2, 'turn guard should prevent double injection')
})

test('context hook on tool-continuation tail (assistant) does not inject', async t => {
  const apiBase = process.env.AIDUMEI_TEST_URL || ''
  if (!apiBase) return t.skip('AIDUMEI_TEST_URL not set')
  const mod = await import('../plugin/index.js')
  const { ctx, hooks } = createFakeCtx()
  await mod.AIDUMEI_PLUGIN.setup(ctx)
  const messages = [
    { role: 'user', content: [{ type: 'text', text: '第一个问题' }] },
    { role: 'assistant', content: [{ type: 'text', text: '第一个回答' }] },
  ]
  await hooks['context']({ messages, sessionID: 'test-ses-tool' })
  const injected = messages.filter(m => m.metadata?.source === 'aidumei-openchamber')
  assert.equal(injected.length, 0, 'should not inject on assistant tail (tool continuation)')
})

test('context hook is a no-op when AIDUMEI_HOOKS=off', async t => {
  const original = process.env.AIDUMEI_HOOKS
  process.env.AIDUMEI_HOOKS = 'off'
  try {
    // Re-import with a fresh module cache to pick up the env change.
    const mod = await import('../plugin/index.js?off-test')
    const { ctx, hooks } = createFakeCtx()
    await mod.AIDUMEI_PLUGIN.setup(ctx)
    assert.equal(hooks['context'], undefined, 'context hook should not be registered when hooks are off')
  } finally {
    if (original === undefined) delete process.env.AIDUMEI_HOOKS
    else process.env.AIDUMEI_HOOKS = original
  }
})
