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
import { randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, rename, rm } from 'node:fs/promises'
import path from 'node:path'
import { atomicJsonWrite, memorySessionId, PROMPT_TTL_MS, stableKey, stateLocation, workspaceAllowed } from '../plugin/runtime.mjs'

function asString(v, fallback = '') {
  return typeof v === 'string' ? v : fallback
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
  const { stateDir, testMode } = stateLocation()
  const configFile = path.join(stateDir, 'config.json')
  let cfg = {}
  try { cfg = JSON.parse(await readFile(configFile, 'utf8')) } catch {}
  if (Boolean(cfg.testMode) !== testMode) throw new Error('state_realm_mismatch')
  if (!cfg.apiBase) throw new Error('config_required')
  const apiBase = (asString(cfg.apiBase).trim() || 'http://127.0.0.1:8767').replace(/\/+$/, '')
  // The token file is authoritative and re-read per request so rotation needs no
  // restart. An inline token is accepted only for configs written by older installers.
  const apiTokenFile = asString(cfg.apiTokenFile).trim()
  if (cfg.apiToken) throw new Error('inline_token_requires_reinstall')
  async function currentToken() {
    if (apiTokenFile) {
      try { return (await readFile(apiTokenFile, 'utf8')).trim() } catch { return '' }
    }
    return testMode ? asString(process.env.AIDUMEI_TEST_TOKEN).trim() : ''
  }
  const userId = asString(cfg.userId).trim() || 'default'
  const bankId = asString(cfg.bankId).trim() || 'default'
  const allowedRoots = Array.isArray(cfg.allowedRoots) ? cfg.allowedRoots : []
  const spoolDir = testMode ? path.join(stateDir, 'spool') : asString(cfg.spoolDir).trim() || path.join(stateDir, 'spool')
  const idleDistillMs = Number(cfg.idleDistillMs) || 120000
  const promptsDir = path.join(stateDir, 'prompts')

  /** FIFO within the event time window; source stays until the outbox commits. */
  async function peekPrompt(sessionId, eventAt, directory) {
    const matches = []
    for (const name of (await readdir(promptsDir)).filter(name => name.endsWith('.json'))) {
      const file = path.join(promptsDir, name)
      try {
        const data = JSON.parse(await readFile(file, 'utf8'))
        if (Number(data.at) < Date.now() - PROMPT_TTL_MS) { await rm(file); continue }
        if (data.sessionId !== sessionId || Number(data.at) > eventAt) continue
        if (!workspaceAllowed(data.directory || directory, allowedRoots)) continue
        matches.push({ ...data, file })
      } catch { /* Retain unreadable files for review. */ }
    }
    return matches.sort((a, b) => a.at - b.at || a.file.localeCompare(b.file))[0] || null
  }

  const pendingDir = path.join(spoolDir, 'pending')
  const receiptsDir = path.join(spoolDir, 'receipts')
  const plansDir = path.join(spoolDir, 'distill')
  await mkdir(pendingDir, { recursive: true, mode: 0o700 })
  await mkdir(receiptsDir, { recursive: true, mode: 0o700 })
  await mkdir(promptsDir, { recursive: true, mode: 0o700 })
  await mkdir(plansDir, { recursive: true, mode: 0o700 })

  /** Spool a payload durably before forwarding. */
  async function spoolWrite(key, payload) {
    const file = path.join(pendingDir, `${key}.json`)
    if (await readFile(file, 'utf8').then(() => true, () => false)) return file
    await atomicJsonWrite(file, payload)
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
          : { source: 'openchamber-ingest', session_id: payload.sessionId, _origin_session_id: payload.sessionId, turn_id: payload.turnId },
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

  // Persist the plan before acknowledging the host event. Re-arming after a
  // restart or transient failure preserves the same revision until new input.
  const distillTimers = new Map()
  const planFile = sessionId => path.join(plansDir, `${stableKey(sessionId)}.json`)
  async function currentPlan(plan) {
    try { return JSON.parse(await readFile(planFile(plan.sessionId), 'utf8')).revision === plan.revision } catch { return false }
  }
  function armDistill(plan) {
    const { sessionId, directory } = plan
    const prev = distillTimers.get(sessionId)
    if (prev) clearTimeout(prev)
    distillTimers.set(sessionId, setTimeout(async () => {
      distillTimers.delete(sessionId)
      if (!await currentPlan(plan)) return
      if (!workspaceAllowed(directory, allowedRoots)) return
      try {
        // Summaries must see every committed turn: deliver the session's turns first.
        await drain()
        if (await hasPending(sessionId)) { armDistill({ ...plan, dueAt: Date.now() + RETRY_DELAY_MS }); return }
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
        if (!['ok', 'skipped'].includes(result?.status)) throw new Error('invalid_distill_acknowledgement')
        if (await currentPlan(plan)) await rm(planFile(sessionId))
      } catch (error) {
        console.error('[aidumei-och] distill_failed', error?.status ? `status=${error.status}` : (error?.name || 'Error'))
        if (await currentPlan(plan)) armDistill({ ...plan, dueAt: Date.now() + RETRY_DELAY_MS })
      }
    }, Math.max(0, plan.dueAt - Date.now())))
  }
  async function scheduleDistill(sessionId, directory) {
    const plan = { sessionId, directory, dueAt: Date.now() + idleDistillMs, revision: randomUUID() }
    await atomicJsonWrite(planFile(sessionId), plan)
    armDistill(plan)
  }

  let handling = Promise.resolve()

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
      void readBody(req).then((body) => { handling = handling.then(async () => {
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
          if (!workspaceAllowed(event.project?.directory, allowedRoots) || event.session?.parentId) {
            json(res, 200, { ok: true, skipped: 'workspace_or_child_session' }); return
          }
          if (event.type === 'turn-finished' && content?.type === 'turn-finished') {
            const rawSessionId = event.session?.id
            if (!rawSessionId) { json(res, 400, { ok: false, error: 'session_required' }); return }
            const sessionId = memorySessionId(rawSessionId)
            const key = `och-${stableKey(userId, bankId, event.id).slice(0, 64)}`
            const previous = await readFile(path.join(pendingDir, `${key}.json`), 'utf8').catch(() => null)
              || await readFile(path.join(receiptsDir, `${key}.json`), 'utf8').catch(() => null)
              || await readFile(path.join(pendingDir, `${key}.json.held`), 'utf8').catch(() => null)
            if (previous) {
              const payload = JSON.parse(previous)
              if (payload.promptFile) await rm(payload.promptFile, { force: true })
              await scheduleDistill(sessionId, event.project.directory)
              void drain()
              json(res, 200, { ok: true, replay: true }); return
            }
            const prompt = await peekPrompt(rawSessionId, Number(event.at) || Date.now(), event.project.directory)
            // Never log conversation text: service logs are not a memory store.
            const messages = []
            if (prompt?.text) messages.push({ role: 'user', content: prompt.text })
            if (content.reply) messages.push({ role: 'assistant', content: content.reply })
            if (messages.length < 2) { json(res, 200, { ok: false, error: 'prompt_pair_unavailable' }); return }
            await spoolWrite(key, { key, sessionId, messages, turnId: prompt.turnId, promptFile: prompt.file, directory: event.project.directory, endReason: 'completed' })
            await scheduleDistill(sessionId, event.project.directory)
            await rm(prompt.file, { force: true })
            void drain()
          } else if (event.type === 'turn-failed') {
            // Failed turns carry the error; nothing durable to store in memory.
            json(res, 200, { ok: true })
            return
          }
          json(res, 200, { ok: true })
        } catch (error) {
          console.error('[aidumei-och] event_persistence_failed', error?.code || error?.name || 'Error')
          json(res, 500, { ok: false, error: 'event_persistence_failed' })
        }
      }); return handling }).catch(() => json(res, 500, { ok: false, error: 'read_failed' }))
      return
    }
    json(res, 404, { error: 'not-found' })
  })

  server.listen(port, '127.0.0.1', () => {
    console.log(`[aidumei-och] service listening on 127.0.0.1:${port}`)
  })

  // Recover an event interrupted between outbox commit and plan commit.
  for (const name of (await readdir(pendingDir)).filter(name => name.endsWith('.json'))) {
    try {
      const job = JSON.parse(await readFile(path.join(pendingDir, name), 'utf8'))
      if (job.kind !== 'summary' && job.directory && !await readFile(planFile(job.sessionId), 'utf8').then(() => true, () => false)) {
        await scheduleDistill(job.sessionId, job.directory)
      }
    } catch { console.error('[aidumei-och] turn_plan_recovery_failed') }
  }
  void drain()
  for (const name of (await readdir(plansDir)).filter(name => name.endsWith('.json'))) {
    try { armDistill(JSON.parse(await readFile(path.join(plansDir, name), 'utf8'))) } catch { console.error('[aidumei-och] distill_plan_unreadable') }
  }
}

main().catch(error => {
  console.error('[aidumei-och] fatal', error?.code || error?.name || 'Error')
  process.exit(1)
})
