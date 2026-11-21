#!/usr/bin/env node
/**
 * aiduMEI OpenChamber adapter — install / check / uninstall manager.
 *
 * Unlike the DSH manager (which writes into OpenChamber-style profile configs),
 * OpenChamber extensions install via its own Settings → Extensions UI or CLI.
 * This manager:
 *   install   — validates the environment, writes the plugin into the OpenCode
 *               plugin directory, writes the shared config (apiBase/token/scope)
 *               for the service, and prints what the user must approve in the UI.
 *   check     — verifies plugin presence, config validity, API health, and that
 *               the service can reach the API.
 *   uninstall — removes the plugin directory and the shared config.
 *
 * Env / flags:
 *   --api URL              aiduMEI API base (install; default http://127.0.0.1:8767)
 *   --user ID --bank ID    memory scope (install)
 *   --token-file PATH      token file (install; required for production)
 *   --test-url URL         test mode (isolated tests only)
 *   --test-token PATH      test token file
 *   --opencode-config DIR  OpenCode config dir (default ~/.config/opencode)
 *   --state-dir DIR        shared state dir (default ~/.aidumei-openchamber)
 *   --yes                  skip confirmation prompts for uninstall
 */

import { access, mkdir, readFile, rm, stat, writeFile, readdir } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const FIXTURE_VERSION = '0.9.0'
const OPENCODE_PLUGIN_DIR_NAME = 'aidumei-openchamber'

function asString(v, fallback = '') {
  return typeof v === 'string' ? v : fallback
}

function parseArgs(argv) {
  const opts = {}
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg.startsWith('--')) {
      const key = arg.slice(2)
      const next = argv[i + 1]
      if (next === undefined || next.startsWith('--')) {
        opts[key] = true
      } else {
        opts[key] = next
        i += 1
      }
    }
  }
  return opts
}

async function exists(p) {
  try { await stat(p); return true } catch { return false }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  const action = ['install', 'check', 'uninstall'].find(k => opts[k] === true)
    || (['install', 'check', 'uninstall'].includes(process.argv[2]?.replace(/^--/, '')) ? process.argv[2].replace(/^--/, '') : undefined)
  if (!action || !['install', 'check', 'uninstall'].includes(action)) {
    console.error('Usage: manage.mjs install|check|uninstall [options]')
    process.exit(1)
  }

  const packageDir = path.dirname(fileURLToPath(import.meta.url))
  const opencodeConfigDir = asString(opts['opencode-config-dir']) || path.join(os.homedir(), '.config', 'opencode')
  const stateDir = asString(opts['state-dir']) || path.join(os.homedir(), '.aidumei-openchamber')
  const pluginTarget = path.join(opencodeConfigDir, 'plugins', OPENCODE_PLUGIN_DIR_NAME)
  const configFile = path.join(stateDir, 'config.json')

  if (action === 'install') {
    const testUrl = asString(opts['test-url'])
    const testTokenFile = asString(opts['test-token'])
    const isTest = Boolean(testUrl)
    const apiBase = (isTest ? testUrl : asString(opts.api) || 'http://127.0.0.1:8767').replace(/\/+$/, '')
    const tokenFile = isTest ? testTokenFile : asString(opts['token-file'])
    const userId = asString(opts.user) || 'default'
    const bankId = asString(opts.bank) || 'default'

    if (!isTest && !tokenFile) {
      console.error('install requires --token-file for production (or --test-url/--test-token for tests)')
      process.exit(1)
    }
    if (tokenFile) {
      try {
        const content = (await readFile(tokenFile, 'utf8')).trim()
        if (!content) throw new Error('empty')
      } catch (e) {
        console.error(`token file unreadable: ${tokenFile} (${e.message})`)
        process.exit(1)
      }
    }

    // Verify the aiduMEI API is reachable before installing.
    const token = tokenFile ? (await readFile(tokenFile, 'utf8')).trim() : ''
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 8000)
      const response = await fetch(`${apiBase}/health`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
      clearTimeout(timer)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
    } catch (e) {
      console.error(`aiduMEI API unreachable at ${apiBase}: ${e.message}`)
      process.exit(1)
    }

    // Copy the plugin into the OpenCode plugin dir.
    const sourcePlugin = path.join(packageDir, 'plugin')
    await rm(pluginTarget, { recursive: true, force: true })
    await mkdir(path.dirname(pluginTarget), { recursive: true, mode: 0o700 })
    await mkdir(pluginTarget, { recursive: true, mode: 0o700 })
    await writeFile(path.join(pluginTarget, 'index.js'), await readFile(path.join(sourcePlugin, 'index.js'), 'utf8'))

    // Write the shared config for the service.
    await mkdir(stateDir, { recursive: true, mode: 0o700 })
    const serviceConfig = {
      apiBase,
      // Only the token PATH is stored; the service and plugin read the file.
      apiTokenFile: tokenFile || undefined,
      userId,
      bankId,
      testMode: isTest,
    }
    await writeFile(configFile, JSON.stringify(serviceConfig, null, 2), { encoding: 'utf8', mode: 0o600 })

    console.log(JSON.stringify({
      ok: true,
      action: 'install',
      pluginTarget,
      configFile,
      apiBase,
      userId,
      bankId,
      testMode: isTest,
      next: 'Restart OpenCode/OpenChamber so the plugin loads; approve the extension in Settings → Extensions (service + sessionEvents lines).',
    }))
    return
  }

  if (action === 'check') {
    const checks = {}
    let ok = true
    checks.pluginInstalled = await exists(path.join(pluginTarget, 'index.js'))
    if (!checks.pluginInstalled) ok = false
    let config = null
    try { config = JSON.parse(await readFile(configFile, 'utf8')) } catch { config = null }
    checks.configValid = Boolean(config?.apiBase)
    if (!checks.configValid) ok = false
    if (config) {
      try {
        const controller = new AbortController()
        const timer = setTimeout(() => controller.abort(), 8000)
        const headers = {}
        let checkToken = ''
        if (config.apiTokenFile) {
          try { checkToken = (await readFile(config.apiTokenFile, 'utf8')).trim() } catch { checkToken = '' }
          checks.tokenFile = Boolean(checkToken)
          if (!checkToken) ok = false
        } else {
          checkToken = asString(config.apiToken).trim()
        }
        // A token copy inside config.json is a legacy layout: report it so it gets removed.
        checks.inlineTokenAbsent = !config.apiToken
        if (config.apiToken) ok = false
        if (checkToken) headers.Authorization = `Bearer ${checkToken}`
        const response = await fetch(`${config.apiBase}/health`, { headers, signal: controller.signal })
        clearTimeout(timer)
        checks.apiHealth = response.ok ? 'ok' : `HTTP ${response.status}`
        if (!response.ok) ok = false
      } catch (e) {
        checks.apiHealth = `unreachable: ${e.message}`
        ok = false
      }
      checks.scope = `${config.userId}/${config.bankId}`
      checks.testMode = config.testMode === true
      // Held jobs are permanent failures waiting for review; they must not stay invisible.
      const pendingDir = path.join(asString(config.spoolDir) || path.join(stateDir, 'spool'), 'pending')
      let names = []
      try { names = await readdir(pendingDir) } catch {}
      checks.pending = names.filter(name => name.endsWith('.json')).length
      checks.held = names.filter(name => name.endsWith('.held')).length
      if (checks.held > 0) ok = false
    } else {
      checks.apiHealth = 'no-config'
      ok = false
    }
    console.log(JSON.stringify({ ok, action: 'check', checks }))
    process.exit(ok ? 0 : 1)
    return
  }

  if (action === 'uninstall') {
    if (!opts.yes) {
      console.error('uninstall requires --yes to confirm removal of the plugin and config')
      process.exit(1)
    }
    let removed = []
    if (await exists(pluginTarget)) {
      await rm(pluginTarget, { recursive: true, force: true })
      removed.push(pluginTarget)
    }
    if (await exists(configFile)) {
      await rm(configFile, { force: true })
      removed.push(configFile)
    }
    // Spool cleanup is opt-in: memory data must never be silently deleted.
    console.log(JSON.stringify({ ok: true, action: 'uninstall', removed, note: 'memory data in aiduMEI API is untouched; spool left in place' }))
    return
  }
}

main().catch(error => {
  console.error('[aidumei-och] manage error:', error.message)
  process.exit(1)
})
