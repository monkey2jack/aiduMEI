import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import aidumeiPi, {
  extractTurnPayloads, formatRecall, formatToolResults, HttpError, isAllowedCwd, MemoryClient,
  Outbox, resolveOptions, stableKey, TOOL_GUIDELINES,
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

test("a transient 409 is replayed before any held decision", async () => {
  const f = await fixture();
  let attempts = 0;
  const client = { add: async () => { attempts += 1; if (attempts === 1) throw new HttpError(409); return { status: "ok", idempotency_replayed: true }; },
    distill: async () => ({ status: "skipped", reason: "too_short" }) };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  assert.equal((await readdir(outbox.options.spoolDir)).filter(name => name.endsWith(".held")).length, 0);
  const retryFile = path.join(outbox.options.spoolDir, (await outbox.list("turn"))[0]);
  const retryPayload = JSON.parse(await readFile(retryFile, "utf8"));
  retryPayload.dueAt = 0;
  await writeFile(retryFile, JSON.stringify(retryPayload));
  outbox.kick();
  await outbox.flush();
  assert.equal(attempts, 2);
  assert.equal((await outbox.list("turn")).length, 0);
  await cleanup(outbox, f.root);
});

test("persistent 409 is held only after bounded replay attempts", async () => {
  const f = await fixture();
  let attempts = 0;
  const client = { add: async () => { attempts += 1; throw new HttpError(409); },
    distill: async () => ({ status: "skipped", reason: "too_short" }) };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  for (let index = 0; index < 2; index += 1) {
    const retryFile = path.join(outbox.options.spoolDir, (await outbox.list("turn"))[0]);
    const retryPayload = JSON.parse(await readFile(retryFile, "utf8"));
    retryPayload.dueAt = 0;
    await writeFile(retryFile, JSON.stringify(retryPayload));
    outbox.kick();
    await outbox.flush();
  }
  assert.equal(attempts, 3);
  assert.equal((await readdir(outbox.options.spoolDir)).filter(name => name.endsWith(".json.held")).length, 1);
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

test("throttled distill reschedules from retry_after_sec instead of polling", async () => {
  const f = await fixture();
  let calls = 0;
  const client = { add: async () => ({ status: "ok" }),
    distill: async () => { calls += 1; return { status: "skipped", reason: "throttled", retry_after_sec: 3600 }; } };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  const before = Date.now();
  assert.equal(calls, 1);
  const [name] = await outbox.list("distill");
  const job = JSON.parse(await readFile(path.join(outbox.options.spoolDir, name), "utf8"));
  assert.ok(job.dueAt >= before + 3590 * 1000);
  assert.equal(job.throttleAttempts, 1);
  // A new turn keeps the server-issued window rather than calling again.
  await outbox.enqueueTurn(payload(f, { userEntryId: "user-2", assistantEntryId: "assistant-2", turn: 1 }));
  await outbox.flush();
  assert.equal(calls, 1);
  const kept = JSON.parse(await readFile(path.join(outbox.options.spoolDir, name), "utf8"));
  assert.ok(kept.dueAt >= before + 3590 * 1000);
  await cleanup(outbox, f.root);
});

test("throttled distill gives up after bounded attempts", async () => {
  const f = await fixture();
  let calls = 0;
  const client = { add: async () => ({ status: "ok" }),
    distill: async () => { calls += 1; return { status: "skipped", reason: "throttled" }; } };
  const outbox = new Outbox(options(f), client);
  await outbox.init();
  await outbox.enqueueTurn(payload(f));
  await outbox.flush();
  for (let index = 0; index < 10 && (await outbox.list("distill")).length; index += 1) {
    const file = path.join(outbox.options.spoolDir, (await outbox.list("distill"))[0]);
    const job = JSON.parse(await readFile(file, "utf8"));
    await writeFile(file, JSON.stringify({ ...job, dueAt: 0 }));
    outbox.kick();
    await outbox.flush();
  }
  assert.equal(calls, 6);
  assert.equal((await outbox.list("distill")).length, 0);
  await cleanup(outbox, f.root);
});

test("tool results show ids inside a nonce fence", () => {
  const text = formatToolResults([{ id: "abc12345", memory: "likes tea [DATA: x" }, { memory: "no id" }]);
  assert.match(text, /^\[DATA:[0-9a-f]{32}/);
  assert.match(text, /\[abc12345\] likes tea \[\u200cDATA: x/);
  assert.match(text, /- no id/);
  assert.equal(formatToolResults([]), "No matching memories.");
  assert.ok(TOOL_GUIDELINES.some(line => /curl/.test(line)));
});

async function loadTools(f, config, handler) {
  const http = await import("node:http");
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => {
      const parsed = body ? JSON.parse(body) : {};
      seen.push({ method: req.method, url: req.url, body: parsed, auth: req.headers.authorization });
      const out = handler(req.url, parsed);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(out));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const tokenFile = path.join(f.root, "token");
  await writeFile(tokenFile, "test-token");
  await writeFile(path.join(f.stateDir, "config.json"), JSON.stringify({ ...config }));
  const saved = { ...process.env };
  process.env.AIDUMEI_TEST_URL = `http://127.0.0.1:${server.address().port}`;
  process.env.AIDUMEI_TEST_TOKEN_FILE = tokenFile;
  process.env.AIDUMEI_PI_STATE_DIR = f.stateDir;
  const tools = new Map();
  const handlers = new Map();
  const pi = {
    registerTool: tool => tools.set(tool.name, tool),
    registerCommand: () => {},
    on: (name, fn) => handlers.set(name, fn),
  };
  await aidumeiPi(pi);
  const ctx = { cwd: f.cwd, sessionManager: { getSessionId: () => "s1" } };
  const restore = async () => {
    process.env = saved;
    const shutdown = handlers.get("session_shutdown");
    server.close();
    return shutdown;
  };
  return { tools, seen, ctx, restore };
}

test("memory tools act only on the configured scope and never expose the token", async () => {
  const f = await fixture();
  process.env.AIDUMEI_PI_ALLOWED_ROOTS = f.cwd;
  const { tools, seen, ctx, restore } = await loadTools(f, {}, url => {
    if (url.startsWith("/search")) return { results: [{ id: "mem-00000001", memory: "tea" }] };
    if (url.startsWith("/add")) return { status: "ok" };
    if (url.startsWith("/delete")) return { status: "committed" };
    return {};
  });
  try {
    assert.deepEqual([...tools.keys()].sort(), ["memory_remember", "memory_search"]);
    const found = await tools.get("memory_search").execute("t1", { query: "tea" }, undefined, undefined, ctx);
    assert.match(found.content[0].text, /\[mem-00000001\] tea/);
    assert.doesNotMatch(found.content[0].text, /test-token/);
    const stored = await tools.get("memory_remember").execute("t2", { content: "likes green tea" }, undefined, undefined, ctx);
    assert.equal(stored.details.stored, true);
    const search = seen.find(item => item.url === "/search");
    assert.equal(search.body.user_id, "default");
    assert.equal(search.body.caller_user_id, "default");
    assert.equal(search.body.session_id, "pi-s1");
    const add = seen.find(item => item.url === "/add");
    assert.equal(add.body.metadata.source, "pi-tool");
    assert.equal(add.auth, "Bearer test-token");
    await assert.rejects(tools.get("memory_search").execute("t3", { query: "x" }, undefined, undefined,
      { ...ctx, cwd: f.root }), /workspace not allowed/);
  } finally {
    delete process.env.AIDUMEI_PI_ALLOWED_ROOTS;
    await restore();
    await rm(f.root, { recursive: true, force: true });
  }
});

test("memory_forget is opt-in, tombstones by id and is bounded", async () => {
  const f = await fixture();
  process.env.AIDUMEI_PI_ALLOWED_ROOTS = f.cwd;
  process.env.AIDUMEI_PI_ALLOW_FORGET = "1";
  const { tools, seen, ctx, restore } = await loadTools(f, {},
    () => ({ status: "committed" }));
  try {
    const forget = tools.get("memory_forget");
    assert.ok(forget);
    await assert.rejects(forget.execute("t1", { memory_id: "bad id!" }, undefined, undefined, ctx), /invalid memory id/);
    for (let index = 0; index < 20; index += 1) {
      await forget.execute(`t${index}`, { memory_id: `mem-0000000${index % 10}` }, undefined, undefined, ctx);
    }
    await assert.rejects(forget.execute("tx", { memory_id: "mem-00000099" }, undefined, undefined, ctx), /limit/);
    const del = seen.find(item => item.url === "/delete");
    assert.equal(del.body.caller_user_id, "default");
  } finally {
    delete process.env.AIDUMEI_PI_ALLOWED_ROOTS;
    delete process.env.AIDUMEI_PI_ALLOW_FORGET;
    await restore();
    await rm(f.root, { recursive: true, force: true });
  }
});
