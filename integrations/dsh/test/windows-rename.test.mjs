// Windows rejects replacing a file another handle has open (EPERM/EACCES/EBUSY).
// The outbox must retry instead of losing the write (CI windows-latest failure
// in reliability.test.mjs "a failed summary write retains its exact payload").
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readdir, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { renameWithRetry } from '../index.mjs'

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

test('persistent failures surface and never leave the temp file behind', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-rename-'))
  t.after(() => rm(dir, { recursive: true, force: true }))
  const temp = path.join(dir, 'job.json.tmp-1')
  await writeFile(temp, '{}')
  const r = flaky(99, 'EPERM')
  await assert.rejects(renameWithRetry(temp, path.join(dir, 'job.json'), { renameFn: r.fn, attempts: 3, baseDelayMs: 1 }), { code: 'EPERM' })
  assert.equal(r.calls(), 3)
  assert.deepEqual(await readdir(dir), [])
})

test('other errors are not retried', async () => {
  const r = flaky(1, 'ENOENT')
  await assert.rejects(renameWithRetry('a', 'b', { renameFn: r.fn, baseDelayMs: 1 }), { code: 'ENOENT' })
  assert.equal(r.calls(), 1)
})
