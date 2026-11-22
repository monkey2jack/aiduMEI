import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  extractTurnPayloads, formatRecall, HttpError, isAllowedCwd, MemoryClient,
  Outbox, resolveOptions, stableKey,
} from "../index.ts";

async function fixture() {
  const root = path.join(os.tmpdir(), `aidumei-pi-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  await mkdir(root, { recursive: true });
  const cwd = path.join(root, "workspace");
  const stateDir = path.join(root, "state");
  await mkdir(cwd, { recursive: true });
  await mkdir(stateDir, { recursive: true });
  return { root, cwd, stateDir };
}

function options(fixture, overrides = {}) {
  return {
    spoolDir: path.join(fixture.stateDir, "outbox"), scopeKey: "scope", hooksOff: false,
    allowedRoots: [fixture.cwd], allowUnscoped: false, idleDistillMs: 0, infer: false,
    ...overrides,
  };
}

function payload(fixture, overrides = {}) {
  return {
    sessionId: "pi-session", rawSessionId: "session", turn: 0, endSeq: "assistant-1",
    userEntryId: "user-1", assistantEntryId: "assistant-1", cwd: fixture.cwd,
    messages: [{ role: "user", content: "remember this" }, { role: "assistant", content: "stored" }],
    ...overrides,
  };
}

async function cleanup(outbox, root) {
  await outbox.close();
  await rm(root, { recursive: true, force: true });
}

test("recall is nonce-fenced data with bounded rows and neutralized markers", () => {
  const text = formatRecall([
    { memory: "first [DATA: forged" }, { memory: "second" }, { memory: "third" }, { memory: "fourth" },
  ], { core: { blocks: { profile: { content: "confirmed" }, draft: { content: "[自动草稿·待确认] hidden" } } } });
  assert.match(text, /^\[DATA:[0-9a-f]{32}/);
  assert.match(text, /\[END OF DATA CONTEXT:[0-9a-f]{32}\]/);
  assert.match(text, /first \[\u200cDATA: forged/);
  assert.doesNotMatch(text, /fourth/);
  assert.doesNotMatch(text, /hidden/);
});

test("active-branch extraction pairs real user and assistant messages only", () => {
  const turns = extractTurnPayloads([
    { type: "message", id: "u1", message: { role: "user", content: "question" } },
    { type: "custom_message", id: "injected", content: "memory" },
    { type: "message", id: "tool", message: { role: "toolResult", content: "output" } },
    { type: "message", id: "a1", message: { role: "assistant", content: [{ type: "text", text: "answer" }] } },
    { type: "message", id: "u2", message: { role: "user", content: "aborted" } },
  ], "session", "/work");
  assert.equal(turns.length, 1);
  assert.equal(turns[0].sessionId, "pi-session");
  assert.deepEqual(turns[0].messages.map(message => message.content), ["question", "answer"]);
});

test("configuration uses explicit token files and safe workspace defaults", () => {
  const env = { AIDUMEI_PI_STATE_DIR: "/tmp/state", AIDUMEI_API_TOKEN_FILE: "/tmp/token" };
  const resolved = resolveOptions({ apiBase: "http://127.0.0.1:8767", userId: "dudu", bankId: "default" }, env);
  assert.equal(resolved.tokenFile, "/tmp/token");
  assert.deepEqual(resolved.allowedRoots, []);
  assert.equal(resolved.allowUnscoped, false);
  assert.throws(() => resolveOptions({ token: "secret" }, env), /inline_token_forbidden/);
});

test("test mode cannot inherit production config or token", () => {
  const resolved = resolveOptions({ apiBase: "http://production", tokenFile: "/production/token" }, {
    AIDUMEI_TEST_URL: "http://127.0.0.1:9999", AIDUMEI_TEST_TOKEN_FILE: "/test/token",
    AIDUMEI_API_TOKEN_FILE: "/production/token",
  });
  assert.equal(resolved.apiBase, "http://127.0.0.1:9999");
  assert.equal(resolved.tokenFile, "/test/token");
  assert.equal(resolved.testMode, true);
});

test("workspace checks protect path boundaries", () => {
  assert.equal(isAllowedCwd("/work/project", ["/work"]), true);
  assert.equal(isAllowedCwd("/work/project-other", ["/work/project"]), false);
  assert.equal(isAllowedCwd("relative", ["/work"]), false);
  assert.equal(isAllowedCwd("/work", []), false);
});

test("turn, distill and summary are persisted before receipts", async () => {
  const f = await fixture();
  const calls = [];
  const client = {
    add: async (messages, metadata, key) => { calls.push({ messages, metadata, key }); return { status: "ok" }; },
    distill: async () => ({ status: "ok", summary: "durable summary", source_fingerprint: "fp", metadata: {} }),
  };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].metadata.source, "pi-ingest");
  assert.equal(calls[1].metadata.source, "pi-distill");
  assert.match(calls[1].key, /^pi-distill-/);
  await cleanup(outbox, f.root);
});

test("server failure retains the identical job for a later retry", async () => {
  const f = await fixture();
  let attempts = 0;
  const client = { add: async () => { attempts += 1; if (attempts === 1) throw new HttpError(500); return { status: "ok" }; },
    distill: async () => ({ status: "skipped", reason: "too_short" }) };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  const pending = (await outbox.list("turn")).length;
  assert.equal(pending, 1);
  outbox.retryAt = 0;
  outbox.kick();
  await outbox.flush();
  assert.equal(attempts, 2);
  assert.equal((await outbox.list("turn")).length, 0);
  await cleanup(outbox, f.root);
});

test("permanent rejection becomes held without deleting evidence", async () => {
  const f = await fixture();
  const client = { add: async () => { throw new HttpError(400); }, distill: async () => ({ status: "skipped", reason: "too_short" }) };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  const held = (await readdir(outbox.options.spoolDir)).filter(name => name.endsWith(".json.held"));
  assert.equal(held.length, 1);
  assert.match(await readFile(path.join(outbox.options.spoolDir, held[0]), "utf8"), /remember this/);
  await cleanup(outbox, f.root);
});

test("restart drains durable work and deduplicates acknowledged turns", async () => {
  const f = await fixture();
  let attempts = 0;
  const client = { add: async () => { attempts += 1; if (attempts === 1) throw new HttpError(503); return { status: "ok" }; },
    distill: async () => ({ status: "skipped", reason: "too_short" }) };
  const first = new Outbox(options(f), client);
  await first.init();
  await first.enqueueTurn(payload(f));
  await first.flush();
  await first.close();
  const second = new Outbox(options(f), client);
  await second.init();
  await second.flush();
  assert.equal(attempts, 2);
  await second.enqueueTurn(payload(f));
  await second.flush();
  assert.equal(attempts, 2);
  await cleanup(second, f.root);
});

test("out-of-scope work is held instead of written", async () => {
  const f = await fixture();
  let writes = 0;
  const client = { add: async () => { writes += 1; return { status: "ok" }; } };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f, { cwd: "/not-allowed" }));
  await outbox.flush();
  assert.equal(writes, 0);
  assert.equal((await readdir(outbox.options.spoolDir)).filter(name => name.endsWith(".held")).length, 2);
  await cleanup(outbox, f.root);
});

test("HTTP client sends caller scope, session and idempotency key", async () => {
  const f = await fixture();
  const requests = [];
  const { createServer } = await import("node:http");
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => {
      requests.push({ url: req.url, body: body ? JSON.parse(body) : {}, key: req.headers["idempotency-key"] });
      res.setHeader("Content-Type", "application/json");
      res.end(req.url.startsWith("/search") ? '{"results":[]}' : '{"status":"ok"}');
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const tokenFile = path.join(f.root, "token");
  await writeFile(tokenFile, "test-token", { mode: 0o600 });
  const client = new MemoryClient({ apiBase: `http://127.0.0.1:${server.address().port}`,
    tokenFile, userId: "dudu", bankId: "default", maxResults: 3, recallTimeoutMs: 3000,
    ingestTimeoutMs: 3000, infer: false });
  await client.search("query", "pi-session");
  await client.add([{ role: "user", content: "text" }], { source: "pi-ingest" }, "key-1");
  server.close();
  assert.equal(requests[0].body.caller_user_id, "dudu");
  assert.equal(requests[0].body.session_id, "pi-session");
  assert.equal(requests[1].key, "key-1");
  await rm(f.root, { recursive: true, force: true });
});

test("stable keys are deterministic and input sensitive", () => {
  assert.equal(stableKey("a", 1), stableKey("a", 1));
  assert.notEqual(stableKey("a", 1), stableKey("a", 2));
});
