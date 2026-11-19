/**
 * aiduMEI memory bridge — OpenChamber service part.
 * Receives session events (turn-finished / turn-failed) via HTTP POST from
 * OpenChamber, persists them to a local spool (at-least-once locally), and
 * forwards each committed turn to the aiduMEI API (/add) with an idempotency
 * key. Also runs idle distillation 2 minutes after the last event.
 *
 * Env (set by manifest / user):
 *   OPENCHAMBER_SERVICE_PORT / OPENCHAMBER_SERVICE_TOKEN — set by the host
 *   AIDUMEI_HOOKS=off          — disables everything (process exits)
 * All aiduMEI configuration (apiBase, token, scope, spool path) comes from the
 * shared config file ~/.aidumei-openchamber/config.json written by the plugin
 * part — the service env does not inherit the host's variables.
 */

import http from 'node:http'
import { appendFile, mkdir, readFile, readdir, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'

const SERVICE_INSTRUCTIONS_MAX = 20000

function asString(v, fallback = '') {
  return typeof v === 'string' ? v : fallback
}

function textFromContent(content) {
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .filter(block => block && block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text)
    .join(' ')
    .trim()
}

function createHashAlgo(algo) {
  // lazy require keeps the module import-free for host-side loading
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  return import('node:crypto').then(m => m.createHash(algo))
}

function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(body))
}

async function readBody(req) {
  let body = ''
  for await (const chunk of req) body += chunk
  return body
}

async function main() {
  const hooksOff = /^(off|0|false)$/i.test(asString(process.env.AIDUMEI_HOOKS).trim())
  const port = Number(process.env.OPENCHAMBER_SERVICE_PORT)
  const serviceToken = asString(process.env.OPENCHAMBER_SERVICE_TOKEN)
  if (hooksOff) {
    console.log('[aidumei-och] AIDUMEI_HOOKS=off — exiting')
    return
  }
  if (!port || !serviceToken) {
    console.error('[aidumei-och] OPENCHAMBER_SERVICE_PORT and OPENCHAMBER_SERVICE_TOKEN are required')
    process.exit(1)
  }

  // The service env does NOT inherit the host's variables (only PATH/HOME/temp/
  // locale). All aiduMEI configuration lives in a shared config file that the
  // plugin part writes (both parts run as the same user; HOME is passed through).
  // HOME is honored explicitly: OpenChamber spawns this service with a scrubbed
  // env (HOME passes through), and tests override HOME to isolate state.
  const homeDir = asString(process.env.HOME).trim() || os.homedir()
  const stateDir = path.join(homeDir, '.aidumei-openchamber')
  const configFile = path.join(stateDir, 'config.json')
  let cfg = {}
  try { cfg = JSON.parse(await readFile(configFile, 'utf8')) } catch {}
  const apiBase = (asString(cfg.apiBase).trim() || 'http://127.0.0.1:8767').replace(/\/+$/, '')
  let apiToken = asString(cfg.apiToken).trim()
  if (!apiToken && cfg.apiTokenFile) {
    try { apiToken = (await readFile(asString(cfg.apiTokenFile), 'utf8')).trim() } catch {}
  }
  const userId = asString(cfg.userId).trim() || 'default'
  const bankId = asString(cfg.bankId).trim() || 'default'
  const testMode = cfg.testMode === true
  const spoolDir = asString(cfg.spoolDir).trim() || path.join(stateDir, 'spool')
  const idleDistillMs = Number(cfg.idleDistillMs) || 120000
  const promptsDir = path.join(stateDir, 'prompts')

  /** Pop the most recent user prompt for a session (written by the plugin part). */
  async function takePrompt(sessionId) {
    try {
      const file = path.join(promptsDir, `${sessionId.replace(/[^A-Za-z0-9_-]/g, '_')}.json`)
      const raw = await readFile(file, 'utf8')
      await rm(file)
      return JSON.parse(raw)
    } catch (error) {
      console.error('[aidumei-och] takePrompt failed:', error instanceof Error ? error.message : String(error), 'file:', path.join(promptsDir, `${sessionId.replace(/[^A-Za-z0-9_-]/g, '_')}.json`))
      return null
    }
  }

  const pendingDir = path.join(spoolDir, 'pending')
  const receiptsDir = path.join(spoolDir, 'receipts')
  await mkdir(pendingDir, { recursive: true, mode: 0o700 })
  await mkdir(receiptsDir, { recursive: true, mode: 0o700 })
  await mkdir(promptsDir, { recursive: true, mode: 0o700 })

  /** Spool a payload durably before forwarding. */
  async function spoolWrite(key, payload) {
    const file = path.join(pendingDir, `${key}.json`)
    const tmp = `${file}.tmp-${process.pid}`
    await appendFile(tmp, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 })
    await rename(tmp, file)
    return file
  }

  async function aidumeiFetch(pathName, { method = 'GET', body, idempotencyKey, timeoutMs = 30000 } = {}) {
    const headers = { Accept: 'application/json', 'Content-Type': 'application/json' }
    if (apiToken) headers.Authorization = `Bearer ${apiToken}`
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error('timeout')), timeoutMs)
    try {
      const response = await fetch(`${apiBase}${pathName}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        redirect: 'error',
      })
      const text = await response.text()
      let parsed = {}
      if (text.trim()) { try { parsed = JSON.parse(text) } catch {} }
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return parsed
    } finally {
      clearTimeout(timer)
    }
  }

  /** Forward one spooled turn to aiduMEI /add with idempotency. */
  async function forwardTurn(file) {
    const raw = await readFile(file, 'utf8')
    const payload = JSON.parse(raw)
    const result = await aidumeiFetch('/add', {
      method: 'POST',
      body: {
        messages: payload.messages,
        user_id: userId,
        caller_user_id: userId,
        bank_id: bankId,
        infer: false,
        async_mode: false,
        idempotency_key: payload.key,
        metadata: { source: 'openchamber-ingest', session_id: payload.sessionId },
      },
      idempotencyKey: payload.key,
    })
    // Aided by the stable idempotency key: replays return the stored result.
    await rename(file, path.join(receiptsDir, `${payload.key}.json`))
    return result
  }

  /** Drain pending spool on startup and after each new event. */
  let draining = Promise.resolve()
  function drain() {
    draining = draining.then(async () => {
      let files
      try { files = (await readdir(pendingDir)).filter(f => f.endsWith('.json')).sort() } catch { return }
      for (const f of files) {
        try { await forwardTurn(path.join(pendingDir, f)) } catch (error) { console.error('[aidumei-och] forward failed:', error?.message || error); /* leave in spool; retried on next drain */ }
      }
    }).catch(() => {})
    return draining
  }

  // Idle distillation: 2 minutes after the latest event, POST /session/distill.
  const distillTimers = new Map()
  function scheduleDistill(sessionId, directory) {
    const prev = distillTimers.get(sessionId)
    if (prev) clearTimeout(prev)
    distillTimers.set(sessionId, setTimeout(async () => {
      distillTimers.delete(sessionId)
      try {
        const q = new URLSearchParams({ session_id: sessionId, user_id: userId, caller_user_id: userId, bank_id: bankId, force: 'false' })
        await aidumeiFetch(`/session/distill?${q}`, { method: 'POST', body: {}, timeoutMs: 45000 })
      } catch { /* distill is best-effort */ }
    }, idleDistillMs))
  }

  const server = http.createServer((req, res) => {
    if (req.headers.authorization !== `Bearer ${serviceToken}`) {
      json(res, 401, { error: 'unauthorized' })
      return
    }
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    if (url.pathname === '/health') {
      json(res, 200, { ok: true })
      return
    }
    if (url.pathname === '/session-events' && req.method === 'POST') {
      void readBody(req).then(async (body) => {
        let event, content
        try {
          const parsed = JSON.parse(body)
          event = parsed.event
          content = parsed.content
        } catch {
          json(res, 400, { ok: false, error: 'bad json' })
          return
        }
        if (!event?.id || !event?.type) {
          json(res, 400, { ok: false, error: 'not a session event' })
          return
        }
        try {
          if (event.type === 'turn-finished' && content?.type === 'turn-finished') {
            const sessionId = event.session?.id || 'unknown'
            const prompt = await takePrompt(sessionId)
            console.log('[aidumei-och] turn-finished:', sessionId, 'prompt-found:', Boolean(prompt), 'prompt-text:', asString(prompt?.text).slice(0, 50))
            const messages = []
            if (prompt?.text) messages.push({ role: 'user', content: prompt.text })
            if (content.reply) messages.push({ role: 'assistant', content: content.reply })
            if (messages.length < 2) { json(res, 200, { ok: true }); return }
            const key = `och-${(event.id || `${sessionId}-${Date.now()}`).slice(0, 80)}`
            await spoolWrite(key, { key, sessionId, messages, endReason: 'completed' })
            void drain()
            scheduleDistill(sessionId, event.project?.directory)
          } else if (event.type === 'turn-failed') {
            // Failed turns carry the error; nothing durable to store in memory.
            json(res, 200, { ok: true })
            return
          }
          json(res, 200, { ok: true })
        } catch (error) {
          console.error('[aidumei-och] event handler failed:', error instanceof Error ? error.message : String(error))
          json(res, 200, { ok: false, error: error instanceof Error ? error.message : String(error) })
        }
      }).catch(() => json(res, 500, { ok: false, error: 'read failed' }))
      return
    }
    json(res, 404, { error: 'not-found' })
  })

  server.listen(port, '127.0.0.1', () => {
    console.log(`[aidumei-och] service listening on 127.0.0.1:${port}, api=${apiBase}, user=${userId}`)
  })

  // Drain anything left from a previous run.
  void drain()
}

main().catch(error => {
  console.error('[aidumei-och] fatal:', error)
  process.exit(1)
})
