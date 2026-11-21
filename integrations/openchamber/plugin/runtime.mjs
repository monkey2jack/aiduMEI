import { createHash, randomUUID } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { mkdir, open, rename, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

export const string = value => typeof value === 'string' ? value : ''
export const stableKey = (...parts) => createHash('sha256').update(JSON.stringify(parts)).digest('hex')
export const memorySessionId = value => `och-${string(value)}`
export const PROMPT_TTL_MS = 24 * 60 * 60 * 1000

// HTTP, config, prompts, plans, outbox and receipts must select the same realm.
// A scrubbed host service must opt into tests explicitly (--test); the default
// service never reads the test realm, even when a test plugin is running.
export function stateLocation(env = process.env, argv = process.argv.slice(2)) {
  const testMode = argv.includes('--test') || Boolean(string(env.AIDUMEI_TEST_URL).trim())
  const home = string(env.HOME).trim() || os.homedir()
  const root = string(env.AIDUMEI_STATE_DIR).trim() || path.join(home, '.aidumei-openchamber')
  return { testMode, stateDir: testMode ? path.join(root, 'test') : root }
}

export function canonicalDirectory(directory) {
  if (!string(directory).trim()) return null
  try { return realpathSync.native(directory) } catch { return null }
}

export function workspaceAllowed(directory, allowedRoots = []) {
  const actual = canonicalDirectory(directory)
  if (!actual || !Array.isArray(allowedRoots)) return false
  return allowedRoots.some(root => {
    const allowed = canonicalDirectory(root)
    if (!allowed) return false
    // realpath resolves symlinks, junctions and Windows short names before the
    // containment check; path.relative also respects the platform's case rules.
    const relative = path.relative(allowed, actual)
    return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
  })
}

export async function atomicJsonWrite(file, value) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 })
  const temp = `${file}.tmp-${process.pid}-${randomUUID()}`
  let handle
  try {
    handle = await open(temp, 'wx', 0o600)
    await handle.writeFile(JSON.stringify(value), 'utf8')
    await handle.sync()
    await handle.close()
    handle = null
    await rename(temp, file)
  } finally {
    await handle?.close().catch(() => {})
    // Only this function's temporary file may be removed on failure.
    await rm(temp, { force: true }).catch(() => {})
  }
}
