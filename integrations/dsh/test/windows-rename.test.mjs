// Windows rejects replacing a file another handle has open (EPERM/EACCES/EBUSY).
// The outbox must retry instead of losing the write (CI windows-latest failure
// in reliability.test.mjs "a failed summary write retains its exact payload").
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { __test, renameWithRetry, resolveOptions } from '../index.mjs'

function flaky(failures, code) {
  let calls = 0
  const fn = async () => {
    calls += 1
    if (calls <= failures) { const error = new Error(code); error.code = code; throw error }
  }
  return { fn, calls: () => calls }
}

test('transient Windows sharing violations are retried until the rename lands', async () => {
  for (const code of ['EPERM', 'EACCES', 'EBUSY']) {
    const r = flaky(3, code)
    await renameWithRetry('a', 'b', { renameFn: r.fn, baseDelayMs: 1 })
    assert.equal(r.calls(), 4, code)
  }
})

test('persistent rename failures preserve the only durable source', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-rename-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  const temp = path.join(dir, 'job.json.tmp-1')
  await writeFile(temp, '{}')
  const r = flaky(99, 'EPERM')
  await assert.rejects(renameWithRetry(temp, path.join(dir, 'job.json'), { renameFn: r.fn, attempts: 3, baseDelayMs: 1 }), { code: 'EPERM' })
  assert.equal(r.calls(), 3)
  assert.deepEqual(await readdir(dir), ['job.json.tmp-1'])
  assert.equal(await readFile(temp, 'utf8'), '{}')
})

test('atomic writes clean only their own temporary file and retain the old destination', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-atomic-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  const file = path.join(dir, 'job.json')
  await writeFile(file, '{"old":"durable"}')
  const r = flaky(99, 'EPERM')
  await assert.rejects(__test.atomicJsonWrite(file, { new: 'pending' }, {
    renameFn: r.fn, attempts: 3, baseDelayMs: 1,
  }), { code: 'EPERM' })
  assert.deepEqual(await readdir(dir), ['job.json'])
  assert.equal(await readFile(file, 'utf8'), '{"old":"durable"}')
})

test('failed hold retains exact turn and summary jobs without creating receipts', async t => {
  for (const kind of ['turn', 'summary']) {
    for (const code of ['EPERM', 'EACCES', 'EBUSY']) {
      const dir = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-held-'))
      t.after(() => rm(dir, { recursive: true, force: true }))
      const options = resolveOptions({ spoolDir: dir, allowUnscoped: true }, {})
      const failure = flaky(99, code)
      const box = new __test.Outbox(options, {}, { warn() {} }, {
        renameFn: failure.fn, attempts: 3, baseDelayMs: 1,
      })
      await box.init()
      box.stop()
      await box.draining
      const file = path.join(options.spoolDir, `${kind}-unacknowledged.json`)
      const raw = JSON.stringify({ key: 'unacknowledged', sessionId: 'dsh-held',
        ...(kind === 'turn' ? { messages: [{ role: 'user', content: 'complete user turn' }] }
          : { result: { summary: 'complete extracted summary' } }) })
      await writeFile(file, raw)
      await assert.rejects(box.hold(file, 'permanent_rejection'), { code })
      assert.equal(await readFile(file, 'utf8'), raw)
      assert.ok(!(await readdir(options.spoolDir)).some(name => name.endsWith('.held')))
      assert.deepEqual(await readdir(box.receiptDir), [])
      await box.close()
    }
  }
})

test('other errors are not retried', async () => {
  const r = flaky(1, 'ENOENT')
  await assert.rejects(renameWithRetry('a', 'b', { renameFn: r.fn, baseDelayMs: 1 }), { code: 'ENOENT' })
  assert.equal(r.calls(), 1)
})
