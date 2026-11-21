import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveOptions } from '../index.mjs'

const manager = fileURLToPath(new URL('../manage.mjs', import.meta.url))

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'aidumei-dsh-check-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const home = path.join(root, 'dsh-home')
  const dir = path.join(home, 'profiles', 'aidumei-preview')
  const env = { PATH: process.env.PATH, HOME: root, USERPROFILE: root,
    XDG_STATE_HOME: root, LOCALAPPDATA: root, DSH_HOME: home,
    SystemRoot: process.env.SystemRoot, WINDIR: process.env.WINDIR }
  return { root, home, dir, env, async check(extra = {}) {
    const child = spawn(process.execPath, [manager, '--check', '--dsh-home', home,
      '--dsh-cli', path.join(root, 'not-installed')], {
      env: { ...env, ...extra }, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = '', stderr = ''
    child.stdout.on('data', b => { stdout += b })
    child.stderr.on('data', b => { stderr += b })
    const timer = setTimeout(() => child.kill(), 10000)
    const code = await new Promise((resolve, reject) => {
      child.on('error', reject)
      child.on('close', resolve)
    }).finally(() => clearTimeout(timer))
    assert.equal(stderr, '')
    return { code, report: JSON.parse(stdout), stdout }
  } }
}

test('check diagnoses a missing DSH host and profile independently with repair hints', async t => {
  const f = await fixture(t)
  const result = await f.check()
  assert.equal(result.code, 1)
  assert.equal(result.report.checks.dsh, false)
  assert.equal(result.report.details.dsh.code, 'cli_unavailable')
  assert.match(result.report.details.dsh.fix, /Install DSH/)
  assert.equal(result.report.checks.profile, false)
  assert.equal(result.report.details.profile.code, 'configuration_missing')
  assert.match(result.report.details.profile.fix, /Install the adapter/)
  assert.equal(result.report.checks.tokenFile, false)
  assert.equal(result.report.checks.workspace, false)
  assert.equal(result.report.checks.api, false)
})

test('check reports corrupt configuration without losing independent host diagnosis', async t => {
  const f = await fixture(t)
  await mkdir(f.dir, { recursive: true })
  await writeFile(path.join(f.dir, '.aidumei-preview.json'), '{not-json')
  const cli = path.join(f.root, 'host.mjs')
  await writeFile(cli, 'console.log("0.2.2-rc.3")\n')
  const result = await f.check({ DSH_CLI_ENTRY: cli })
  assert.equal(result.code, 1)
  assert.equal(result.report.checks.dsh, true)
  assert.equal(result.report.details.profile.code, 'configuration_unreadable')
  assert.match(result.report.details.profile.fix, /backup/)
  assert.equal(result.report.checks.bundle, false)
})

test('check exposes pending and held work and never treats health as proof of key binding', async t => {
  const f = await fixture(t)
  const server = createServer((req, res) => {
    assert.equal(req.url, '/health')
    assert.equal(req.headers.authorization, 'Bearer synthetic-private-token')
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ health_status: 'ok' }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => server.close(resolve)))
  const cli = path.join(f.root, 'host.mjs')
  await writeFile(cli, 'console.log("0.2.2-rc.3")\n')
  await mkdir(f.dir, { recursive: true })
  const tokenFile = path.join(f.root, 'private.key')
  await writeFile(tokenFile, 'synthetic-private-token')
  const config = { apiBase: `http://127.0.0.1:${server.address().port}`, userId: 'check-user',
    bankId: 'check-bank', tokenFile, allowedRoots: [f.root], spoolDir: path.join(f.root, 'spool') }
  const block = '# synthetic managed block'
  const stateFile = path.join(f.dir, '.aidumei-preview.json')
  const originalState = JSON.stringify({ config, block, status: 'installed' })
  await writeFile(stateFile, originalState)
  await writeFile(path.join(f.dir, 'cordis.patch.yml'), block)
  await writeFile(path.join(f.dir, 'package.json'), JSON.stringify({ dsh: { profile: { bundles: ['@monkey2jack/aidumei-dsh'] } } }))
  const resolved = resolveOptions(config, f.env)
  await mkdir(resolved.spoolDir, { recursive: true })
  await writeFile(path.join(resolved.spoolDir, 'turn-ready.json'), '{}')
  await writeFile(path.join(resolved.spoolDir, 'summary-blocked.json.held'), '{}')
  let result = await f.check({ DSH_CLI_ENTRY: cli })
  assert.equal(result.code, 1)
  assert.equal(result.report.checks.api, true)
  assert.deepEqual(result.report.outbox, { pending: 1, held: 1, observed: true })
  assert.equal(result.report.checks.noHeldJobs, false)
  assert.match(result.report.details.noHeldJobs.fix, /do not delete/)
  assert.equal(result.report.identityBinding.status, 'unverified')
  assert.ok(!result.stdout.includes('synthetic-private-token'))
  assert.ok(!result.stdout.includes(f.root))
  assert.equal(await readFile(stateFile, 'utf8'), originalState)
  await rm(path.join(resolved.spoolDir, 'summary-blocked.json.held'))
  result = await f.check({ DSH_CLI_ENTRY: cli })
  assert.equal(result.code, 0)
  assert.equal(result.report.ok, true)
  assert.equal(result.report.outbox.pending, 1)
  assert.equal(result.report.identityBinding.status, 'unverified')
})
