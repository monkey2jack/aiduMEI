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
import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises'
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

function stableKey(...parts) {
  return createHash('sha256').update(JSON.stringify(parts)).digest('hex')
}

class HttpError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status }
}

// Permanent client errors are held for review instead of being retried forever.
function isPermanent(error) {
  const status = Number(error?.status)
  return status >= 400 && status < 500 && ![408, 409, 429].includes(status)
}

const RETRY_DELAY_MS = 30000

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
  // The token file is authoritative and re-read per request so rotation needs no
  // restart. An inline token is accepted only for configs written by older installers.
  const apiTokenFile = asString(cfg.apiTokenFile).trim()
  const inlineToken = asString(cfg.apiToken).trim()
  async function currentToken() {
    if (apiTokenFile) {
      try { return (await readFile(apiTokenFile, 'utf8')).trim() } catch { return '' }
    }
    return inlineToken
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
      if (error?.code !== 'ENOENT') console.error('[aidumei-och] prompt_unavailable', error?.code || 'error')
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
    if (await readFile(file, 'utf8').then(() => true, () => false)) return file
    const tmp = `${file}.tmp-${process.pid}-${randomUUID()}`
    await writeFile(tmp, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 })
    await rename(tmp, file)
    return file
  }

  async function aidumeiFetch(pathName, { method = 'GET', body, idempotencyKey, timeoutMs = 30000 } = {}) {
    const headers = { Accept: 'application/json', 'Content-Type': 'application/json' }
    const token = await currentToken()
    if (token) headers.Authorization = `Bearer ${token}`
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
      if (!response.ok) throw new HttpError(response.status)
      return parsed
    } finally {
      clearTimeout(timer)
    }
  }

  /** Forward one spooled turn to aiduMEI /add with idempotency. */
  async function forwardTurn(file) {
    const raw = await readFile(file, 'utf8')
    const payload = JSON.parse(raw)
    // A summary job carries the server-sealed metadata from /session/distill;
    // /add only honours the summary identity when that seal is intact.
    const isSummary = payload.kind === 'summary'
    const result = await aidumeiFetch('/add', {
      method: 'POST',
      body: {
        messages: isSummary ? payload.summary : payload.messages,
        user_id: userId,
        caller_user_id: userId,
        bank_id: bankId,
        infer: false,
        async_mode: false,
        idempotency_key: payload.key,
        metadata: isSummary
          ? { ...(payload.metadata || {}), session_id: `distill:${payload.sessionId}` }
          : { source: 'openchamber-ingest', session_id: payload.sessionId },
      },
      idempotencyKey: payload.key,
    })
    if (result?.status !== 'ok') throw new Error('invalid_add_acknowledgement')
    // Aided by the stable idempotency key: replays return the stored result.
    await rename(file, path.join(receiptsDir, `${payload.key}.json`))
    return result
  }

  /** Drain pending spool on startup and after each new event. */
  let draining = Promise.resolve()
  let retryTimer = null
  function drain() {
    draining = draining.then(async () => {
      let files
      try { files = (await readdir(pendingDir)).filter(f => f.endsWith('.json')).sort() } catch { return }
      let retry = false
      for (const f of files) {
        const file = path.join(pendingDir, f)
        try { await forwardTurn(file) } catch (error) {
          if (isPermanent(error) || error instanceof SyntaxError || error?.message === 'invalid_add_acknowledgement') {
            // Keep the exact payload for operator review; never drop it silently.
            await rename(file, `${file}.held`).catch(() => {})
            console.error('[aidumei-och] outbox_held', error?.status ? `status=${error.status}` : (error?.name || 'Error'))
          } else {
            retry = true
            console.error('[aidumei-och] outbox_retry_scheduled', error?.status ? `status=${error.status}` : (error?.name || 'Error'))
            break
          }
        }
      }
      if (retry && !retryTimer) {
        retryTimer = setTimeout(() => { retryTimer = null; void drain() }, RETRY_DELAY_MS)
        retryTimer.unref?.()
      }
    }).catch(() => {})
    return draining
  }

  async function hasPending(sessionId) {
    let files
    try { files = (await readdir(pendingDir)).filter(f => f.endsWith('.json') || f.endsWith('.json.held')) } catch { return false }
    for (const f of files) {
      try { if (JSON.parse(await readFile(path.join(pendingDir, f), 'utf8')).sessionId === sessionId) return true } catch {}
    }
    return false
  }

  // Idle distillation: 2 minutes after the latest event, POST /session/distill.
  const distillTimers = new Map()
  function scheduleDistill(sessionId, directory) {
    const prev = distillTimers.get(sessionId)
    if (prev) clearTimeout(prev)
    distillTimers.set(sessionId, setTimeout(async () => {
      distillTimers.delete(sessionId)
      try {
        // Summaries must see every committed turn: deliver the session's turns first.
        await drain()
        if (await hasPending(sessionId)) { scheduleDistill(sessionId, directory); return }
        const q = new URLSearchParams({ session_id: sessionId, user_id: userId, caller_user_id: userId, bank_id: bankId, force: 'false' })
        const result = await aidumeiFetch(`/session/distill?${q}`, { method: 'POST', body: {}, timeoutMs: 45000 })
        // /session/distill only extracts; the caller must /add the summary
        // (see ducky/routes_v8.py). Without this step the distill line writes nothing.
        if (result?.status === 'ok' && typeof result.summary === 'string' && result.summary) {
          const fingerprint = result.source_fingerprint || stableKey(sessionId, result.summary)
          const key = `och-distill-${stableKey(userId, bankId, sessionId, fingerprint).slice(0, 64)}`
          if (!await readFile(path.join(receiptsDir, `${key}.json`), 'utf8').then(() => true, () => false)) {
            await spoolWrite(key, { kind: 'summary', key, sessionId, summary: result.summary, metadata: result.metadata || {} })
            void drain()
          }
        }
      } catch (error) {
        console.error('[aidumei-och] distill_failed', error?.status ? `status=${error.status}` : (error?.name || 'Error'))
      }
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
            // Never log conversation text: service logs are not a memory store.
            const messages = []
            if (prompt?.text) messages.push({ role: 'user', content: prompt.text })
            if (content.reply) messages.push({ role: 'assistant', content: content.reply })
            if (messages.length < 2) { json(res, 200, { ok: true }); return }
            const key = `och-${stableKey(userId, bankId, event.id).slice(0, 64)}`
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
    console.log(`[aidumei-och] service listening on 127.0.0.1:${port}`)
  })

  // Drain anything left from a previous run.
  void drain()
}

main().catch(error => {
  console.error('[aidumei-och] fatal:', error)
  process.exit(1)
})
