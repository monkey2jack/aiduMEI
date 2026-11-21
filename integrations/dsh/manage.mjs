#!/usr/bin/env node
// Profile changes are backed up first. Credentials are read from files only.
import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { access, mkdir, readFile, readdir, realpath, rename, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { dshCommand } from './cli.mjs'
import { resolveOptions } from './index.mjs'

const packageDir = fileURLToPath(new URL('.', import.meta.url))
const packageName = '@monkey2jack/aidumei-dsh'
const begin = '# BEGIN aidumei-dsh managed preview'
const end = '# END aidumei-dsh managed preview'
const help = `node integrations/dsh/manage.mjs install|--check|uninstall [options]
  --profile NAME       Default: aidumei-preview (headless template)
  --dsh-home PATH      Default: DSH_HOME or ~/.dsh
  --dsh-cli PATH       Desktop dsh.cmd or installed dsh executable
  --workdir PATH       Repeat for each allowed absolute workspace (install)
  --api URL            Explicit aiduMEI API (install)
  --user ID --bank ID   Explicit memory scope (install)
  --token-file PATH    Token file; never put a token on the command line
  --python PATH        Optional absolute Python interpreter for stdio MCP
Set AIDUMEI_TEST_URL and AIDUMEI_TEST_TOKEN_FILE for isolated tests.
Fully quit a profile's DSH processes before changing that profile.`

function parse(argv) {
  const [action, ...rest] = argv
  if (!['install', '--check', 'uninstall', '--help'].includes(action)) throw new Error('invalid_action')
  const opts = { action, profile: 'aidumei-preview', workdir: [] }
  const keys = new Set(['profile', 'dsh-home', 'dsh-cli', 'workdir', 'api', 'user', 'bank', 'token-file', 'python'])
  for (let i = 0; i < rest.length; i++) {
    const key = rest[i].replace(/^--/, '')
    if (!rest[i].startsWith('--') || !keys.has(key) || !rest[i + 1] || rest[i + 1].startsWith('--')) throw new Error('invalid_argument')
    if (key === 'workdir') opts.workdir.push(rest[++i])
    else opts[key] = rest[++i]
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(opts.profile)) throw new Error('invalid_profile')
  return opts
}

async function exists(file) { try { await access(file); return true } catch { return false } }
async function atomic(file, text) {
  const tmp = `${file}.tmp-${randomUUID()}`
  await writeFile(tmp, text, { mode: 0o600 })
  await rename(tmp, file)
}
const digest = text => createHash('sha256').update(text).digest('hex')

async function checkInstallation(opts, { dir, patchFile, stateFile, env }) {
  const checks = {}
  const details = {}
  const record = (name, ok, code, fix) => {
    checks[name] = Boolean(ok)
    details[name] = { ok: Boolean(ok), code, ...(!ok && fix ? { fix } : {}) }
  }
  let version = ''
  try {
    const invocation = await dshCommand({ cli: opts['dsh-cli'], env })
    const child = spawn(invocation.command, [...invocation.args, '--version'], {
      env: invocation.env, stdio: ['ignore', 'pipe', 'ignore'],
    })
    let output = ''
    child.stdout.on('data', b => { if (output.length < 4096) output += b })
    const timer = setTimeout(() => child.kill(), 5000)
    const code = await new Promise((resolve, reject) => {
      child.on('error', reject)
      child.on('close', resolve)
    }).finally(() => clearTimeout(timer))
    if (code === 0 && /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(output.trim())) version = output.trim()
    record('dsh', version === '0.2.2-rc.3', version ? 'version_checked' : 'cli_unavailable',
      version ? 'Use the supported DSH 0.2.2-rc.3 host.' : 'Install DSH or provide --dsh-cli pointing to its executable.')
  } catch {
    record('dsh', false, 'cli_unavailable', 'Install DSH or provide --dsh-cli pointing to its executable.')
  }
  let state, patch = '', manifest
  try {
    state = JSON.parse(await readFile(stateFile, 'utf8'))
    record('profile', Boolean(state && typeof state === 'object' && state.config), 'profile_read',
      'Install the adapter into this profile before checking it.')
  } catch (error) {
    record('profile', false, error?.code === 'ENOENT' ? 'configuration_missing' : 'configuration_unreadable',
      'Install the adapter into this profile; review its private backup if the configuration is damaged.')
  }
  try { patch = await readFile(patchFile, 'utf8') } catch {}
  record('managed', state?.status === 'installed' && typeof state?.block === 'string' && patch.includes(state.block),
    'managed_patch_checked', 'Restore or reinstall the managed patch after reviewing the profile backup.')
  try { manifest = JSON.parse(await readFile(path.join(dir, 'package.json'), 'utf8')) } catch {}
  record('bundle', manifest?.dsh?.profile?.bundles?.includes(packageName) === true,
    'bundle_checked', 'Install or enable the aiduMEI bundle in this DSH profile.')
  let resolved, token = ''
  if (checks.profile) {
    try {
      resolved = resolveOptions(state.config, env)
      record('configuration', true, 'configuration_valid')
    } catch {
      record('configuration', false, 'configuration_invalid', 'Review the endpoint, identity and absolute workspace settings, then reinstall.')
    }
  } else record('configuration', false, 'configuration_missing', 'Install the adapter with explicit API, user, bank and workspace options.')
  if (resolved) {
    try { token = (await readFile(resolved.tokenFile, 'utf8')).trim() } catch {}
    record('tokenFile', Boolean(token), 'token_file_checked', 'Provision a readable nonempty token file and reinstall with --token-file.')
    let workspace = resolved.allowedRoots.length > 0
    for (const root of resolved.allowedRoots) workspace &&= await exists(root)
    record('workspace', workspace, 'workspace_checked', 'Configure at least one existing absolute allowed workspace.')
  } else {
    record('tokenFile', false, 'configuration_required', 'Fix the profile configuration before checking its token file.')
    record('workspace', false, 'configuration_required', 'Fix the profile configuration before checking its workspace allowlist.')
  }
  if (resolved && token) {
    try {
      const response = await fetch(`${resolved.apiBase}/health`, {
        headers: { Authorization: `Bearer ${token}` }, redirect: 'error', signal: AbortSignal.timeout(5000),
      })
      const health = await response.json()
      record('api', response.ok && health.health_status === 'ok', 'api_health_checked',
        'Check the API service, endpoint and token; inspect private server diagnostics if health is degraded.')
    } catch {
      record('api', false, 'api_unavailable', 'Check API connectivity and the configured endpoint; inspect private server diagnostics.')
    }
  } else record('api', false, 'token_and_configuration_required', 'Fix the profile and token file before checking API health.')
  const outbox = { pending: 0, held: 0, observed: false }
  if (resolved) {
    try {
      const names = await readdir(resolved.spoolDir)
      outbox.pending = names.filter(name => /^(turn|summary|distill)-.*\.json$/.test(name)).length
      outbox.held = names.filter(name => name.endsWith('.json.held')).length
      outbox.observed = true
      record('noHeldJobs', outbox.held === 0, 'outbox_checked', 'Archive and inspect held jobs, fix their rejection cause, then retry them; do not delete them to clear this check.')
    } catch (error) {
      outbox.observed = error?.code === 'ENOENT'
      record('noHeldJobs', outbox.observed, outbox.observed ? 'outbox_not_created' : 'outbox_unreadable',
        'Restore access to the profile outbox and inspect its pending and held jobs.')
    }
  } else record('noHeldJobs', false, 'configuration_required', 'Fix the profile configuration before checking its outbox.')
  const ok = Object.values(checks).every(Boolean)
  console.log(JSON.stringify({ ok, action: 'check', dshVersion: version || null,
    testMode: resolved?.testMode ?? null, mcpConfigured: Boolean(state?.python), checks, details, outbox,
    identityBinding: { status: 'unverified', code: 'server_binding_introspection_required',
      fix: 'Provision an identity-bound agent key with enforcement enabled; API health alone cannot verify its binding.' } }))
  if (!ok) process.exitCode = 1
}

async function main() {
  const opts = parse(process.argv.slice(2))
  if (opts.action === '--help') { console.log(help); return }
  const home = path.resolve(opts['dsh-home'] || process.env.DSH_HOME || path.join(os.homedir(), '.dsh'))
  const dir = path.join(home, 'profiles', opts.profile)
  const patchFile = path.join(dir, 'cordis.patch.yml')
  const stateFile = path.join(dir, '.aidumei-preview.json')
  const env = { ...process.env, DSH_HOME: home }
  if (opts.action === '--check') {
    await checkInstallation(opts, { dir, patchFile, stateFile, env })
    return
  }
  const invocation = await dshCommand({ cli: opts['dsh-cli'], env })
  const logs = path.join(home, 'aidumei-install-logs', `${Date.now()}-${randomUUID()}`)
  let logIndex = 0
  async function run(args) {
    const child = spawn(invocation.command, [...invocation.args, ...args], { env: invocation.env, stdio: ['ignore', 'pipe', 'pipe'] })
    let output = ''
    child.stdout.on('data', b => { output += b })
    child.stderr.on('data', b => { output += b })
    const timer = setTimeout(() => child.kill(), 180000)
    const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve) }).finally(() => clearTimeout(timer))
    await mkdir(logs, { recursive: true, mode: 0o700 })
    await writeFile(path.join(logs, `${++logIndex}.log`), output, { mode: 0o600 })
    if (code !== 0) throw new Error('dsh_command_failed_see_private_install_logs')
    return output
  }
  const version = (await run(['--version'])).trim()
  if (!/^0\.2\.2-rc\.3$/.test(version)) throw new Error('unsupported_dsh_version')
  if (opts.action === 'install') {
    if (await exists(stateFile) && JSON.parse(await readFile(stateFile, 'utf8')).status !== 'uninstalled') throw new Error('already_managed_uninstall_before_reconfigure')
    if (!opts.api || !opts.user || !opts.bank || !opts.workdir.length) throw new Error('explicit_api_scope_and_workspace_required')
    const roots = []
    for (const root of opts.workdir) {
      if (!path.isAbsolute(root)) throw new Error('absolute_workspace_required')
      roots.push(await realpath(root))
    }
    const config = { apiBase: opts.api, userId: opts.user, bankId: opts.bank, allowedRoots: roots,
      instanceId: opts.profile, allowUnscoped: false }
    if (opts['token-file']) {
      if (!path.isAbsolute(opts['token-file'])) throw new Error('absolute_token_file_required')
      config.tokenFile = opts['token-file']
    }
    const resolved = resolveOptions(config, env)
    if (resolved.testMode && !env.AIDUMEI_TEST_TOKEN_FILE) throw new Error('test_token_file_required')
    const tokenFile = resolved.tokenFile
    if (!tokenFile) throw new Error('token_file_required')
    if (!(await readFile(tokenFile, 'utf8')).trim()) throw new Error('empty_token_file')
    if (opts.python) {
      if (!path.isAbsolute(opts.python)) throw new Error('absolute_python_required')
      await access(opts.python)
    }
    if (!(await exists(path.join(dir, 'package.json')))) {
      if (['desktop', 'headless', 'web', 'acp'].includes(opts.profile.toLowerCase())) throw new Error('initialize_shipped_profile_first')
      await run(['--profile', opts.profile, '--from-default-profile', 'headless', '--dump-config'])
    }
    const original = await readFile(patchFile, 'utf8')
    if (original.includes(begin) || original.includes('aidumei-mcp') || original.includes('aidumei-dsh')) throw new Error('existing_aidumei_patch_requires_review')
    const stripped = original.replace(/^\s*#.*$/gm, '').trim()
    const base = stripped === '[]' ? original.replace(/^\s*\[\]\s*$/m, '') : original
    if (stripped !== '[]' && stripped && !stripped.startsWith('-')) throw new Error('use_yaml_block_sequence_for_profile_patch')
    const rows = [{ id: 'aidumei-dsh', config }]
    if (opts.python) rows.push({ insert: [{ id: 'aidumei-mcp', name: '@deepseek-ai/dsh-mcp-client', config: {
      serverName: 'aidumei', transport: 'stdio', command: opts.python,
      args: [path.join(packageDir, 'mcp-stdio.py')], startupTimeoutMs: 20000,
      env: { AIDUMEM_API_BASE: resolved.apiBase, AIDUMEM_USER_ID: resolved.userId, AIDUMEI_DEFAULT_BANK_ID: resolved.bankId,
        [resolved.testMode ? 'AIDUMEI_TEST_TOKEN_FILE' : 'AIDUMEI_API_TOKEN_FILE']: tokenFile,
        ...(resolved.testMode ? { AIDUMEI_TEST_URL: resolved.apiBase } : {}), PYTHONIOENCODING: 'utf-8' },
    } }] })
    const block = `${begin}\n${rows.map(row => `- ${JSON.stringify(row)}`).join('\n')}\n${end}\n`
    const managed = `${base.trimEnd()}\n${block}`
    const backupDir = path.join(dir, '.cache-backups', `${Date.now()}-${randomUUID()}`)
    await mkdir(backupDir, { recursive: true, mode: 0o700 })
    for (const file of ['package.json', 'cordis.patch.yml', 'pnpm-lock.yaml', 'pnpm-workspace.yaml']) {
      if (await exists(path.join(dir, file))) await writeFile(path.join(backupDir, file), await readFile(path.join(dir, file)), { mode: 0o600 })
    }
    await atomic(stateFile, JSON.stringify({ config, python: opts.python || '', testMode: resolved.testMode,
      tokenFile, backupDir, original, installedHash: digest(managed), block, status: 'installing' }))
    await run(['plugin', '--profile', opts.profile, 'add', packageDir])
    await atomic(patchFile, managed)
    const state = JSON.parse(await readFile(stateFile, 'utf8'))
    await atomic(stateFile, JSON.stringify({ ...state, status: 'installed' }))
    console.log(JSON.stringify({ ok: true, action: 'install', profile: opts.profile, dshVersion: version, mcp: Boolean(opts.python), testMode: resolved.testMode }))
    return
  }
  const state = JSON.parse(await readFile(stateFile, 'utf8'))
  const patch = await readFile(patchFile, 'utf8')
  if (opts.action === 'uninstall') {
    if (state.status === 'uninstalled') throw new Error('already_uninstalled')
    let restored
    if (digest(patch) === state.installedHash) restored = state.original
    else if (patch.includes(state.block)) restored = patch.replace(state.block, '')
    else if (['installing', 'uninstalling'].includes(state.status) && patch === state.original) restored = state.original
    else if (state.status === 'uninstalling' && patch === state.uninstallPatch) restored = patch
    else throw new Error('managed_patch_changed_review_backup_before_uninstall')
    if (!restored.replace(/^\s*#.*$/gm, '').trim()) restored += '\n[]\n'
    await atomic(stateFile, JSON.stringify({ ...state, status: 'uninstalling', uninstallPatch: restored }))
    await atomic(patchFile, restored)
    const manifestFile = path.join(dir, 'package.json')
    let manifest = JSON.parse(await readFile(manifestFile, 'utf8'))
    if (['dependencies', 'devDependencies', 'optionalDependencies'].some(key => Object.hasOwn(manifest[key] || {}, packageName))) {
      await run(['plugin', '--profile', opts.profile, 'remove', packageName])
      manifest = JSON.parse(await readFile(manifestFile, 'utf8'))
    }
    // A killed package removal can leave selection behind after its dependency
    // is gone. Only remove our name; preserve all unrelated manifest settings.
    if (manifest.dsh?.profile?.bundles?.includes(packageName)) {
      manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.filter(name => name !== packageName)
      await atomic(manifestFile, JSON.stringify(manifest, null, 2) + '\n')
    }
    await atomic(stateFile, JSON.stringify({ ...state, status: 'uninstalled' }))
    console.log(JSON.stringify({ ok: true, action: 'uninstall', profile: opts.profile, backupRetained: true, outboxRetained: true }))
    return
  }
}

main().catch(error => {
  // No path, HTTP body, token or third-party error is echoed to the terminal.
  const code = /^[a-z][a-z0-9_]+$/.test(error.message) ? error.message : 'operation_failed_see_profile_backup_and_private_logs'
  console.error(JSON.stringify({ ok: false, error: code }))
  process.exitCode = 1
})
