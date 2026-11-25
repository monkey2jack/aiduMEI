#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { access, mkdir, readFile, readdir, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const packageDir = path.dirname(fileURLToPath(import.meta.url));
const help = `node integrations/pi/manage.mjs install|check|uninstall [options]
  --pi-home PATH       Default: ~/.pi/agent (never a project .pi directory)
  --pi-bin PATH        Default: pi from PATH
  --state-dir PATH     Default: ~/.aidumei-pi
  --api URL            aiduMEI API base (required for production install)
  --user ID --bank ID  Explicit memory scope (required for production install)
  --token-file PATH    Token file; never put a token on the command line
  --workdir PATH       Repeat for each allowed absolute workspace (required)
  --allow-forget       Register the memory_forget tool (tombstoned, restorable)
  --yes                Confirm uninstall
Test isolation: AIDUMEI_TEST_URL and AIDUMEI_TEST_TOKEN_FILE; use temporary --pi-home/--state-dir.`;

function parse(argv) {
  const [action, ...rest] = argv;
  if (!["install", "check", "uninstall", "--help"].includes(action)) throw new Error("invalid_action");
  const opts = { action, workdir: [] };
  const keys = new Set(["pi-home", "pi-bin", "state-dir", "api", "user", "bank", "token-file", "workdir"]);
  for (let index = 0; index < rest.length; index += 1) {
    const raw = rest[index];
    if (raw === "--yes") { opts.yes = true; continue; }
    if (raw === "--allow-forget") { opts.allowForget = true; continue; }
    const key = raw.replace(/^--/, "");
    if (!raw.startsWith("--") || !keys.has(key) || !rest[index + 1] || rest[index + 1].startsWith("--")) {
      throw new Error("invalid_argument");
    }
    if (key === "workdir") opts.workdir.push(rest[++index]);
    else opts[key] = rest[++index];
  }
  return opts;
}

async function exists(file) { try { await stat(file); return true; } catch { return false; } }
async function atomic(file, text, mode = 0o600) {
  const temp = `${file}.tmp-${randomUUID()}`;
  await writeFile(temp, text, { mode });
  await rename(temp, file);
}
function validScope(value) { return /^[A-Za-z0-9._:-]{1,128}$/.test(value); }

function checkPiBinary(piBin) {
  const result = spawnSync(piBin, ["--version"], { encoding: "utf8", timeout: 8000,
    shell: process.platform === "win32" });
  const version = `${result.stdout || ""}${result.stderr || ""}`.trim().split(/\s+/).at(-1) || "";
  const match = version.match(/(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$/);
  if (result.status !== 0 || !match) return { ok: false, version: null };
  const major = Number(match[1]);
  const minor = Number(match[2]);
  return { ok: major > 1 || major === 1 && minor >= 6, version };
}

function tarEntry(name, data) {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, "utf8");
  header.write("0000600\0", 100);
  header.write("0000000\0", 108);
  header.write("0000000\0", 116);
  header.write(`${data.length.toString(8).padStart(11, "0")}\0`, 124);
  header.write(`${Math.floor(Date.now() / 1000).toString(8).padStart(11, "0")}\0`, 136);
  header.write("        ", 148);
  header.write("0", 156);
  header.write("ustar\0", 257);
  header.write("00", 263);
  const checksum = [...header].reduce((sum, byte) => sum + byte, 0);
  header.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148);
  const padding = Buffer.alloc((512 - data.length % 512) % 512);
  return Buffer.concat([header, data, padding]);
}

async function writeBackupArchive(archive, entries) {
  const end = Buffer.alloc(1024);
  const content = Buffer.concat([...entries.map(entry => tarEntry(entry.name, entry.data)), end]);
  await atomic(archive, gzipSync(content));
}

async function backup(piHome, stateDir) {
  const backupDir = path.join(stateDir, "backups");
  await mkdir(backupDir, { recursive: true, mode: 0o700 });
  const archive = path.join(backupDir, `before-change-${Date.now()}-${randomUUID()}.tar.gz`);
  const entries = [];
  const extension = path.join(piHome, "extensions", "aidumei");
  for (const file of ["index.ts", "package.json", "manage.mjs"]) {
    const source = path.join(extension, file);
    if (await exists(source)) entries.push({ name: `extension/${file}`, data: await readFile(source) });
  }
  const config = path.join(stateDir, "config.json");
  if (await exists(config)) entries.push({ name: "config.json", data: await readFile(config) });
  if (entries.length) await writeBackupArchive(archive, entries);
  return entries.length ? archive : null;
}

async function readConfig(stateDir) {
  try {
    const config = JSON.parse(await readFile(path.join(stateDir, "config.json"), "utf8"));
    if (!config.apiBase || !validScope(config.userId || "") || !validScope(config.bankId || "")) throw new Error("invalid");
    return config;
  } catch { return null; }
}

async function check(opts, piHome, stateDir) {
  const checks = {};
  const record = (name, ok, code) => { checks[name] = { ok: Boolean(ok), code }; };
  const pi = checkPiBinary(opts["pi-bin"] || "pi");
  record("pi", pi.ok, pi.version || "cli_unavailable");
  const target = path.join(piHome, "extensions", "aidumei");
  record("extension", await exists(path.join(target, "index.ts")) && await exists(path.join(target, "package.json")), "files_checked");
  const config = await readConfig(stateDir);
  record("configuration", Boolean(config), "configuration_checked");
  let token = "";
  if (config) {
    record("inlineTokenAbsent", !config.apiToken && !config.token, "inline_token_absent");
    try { token = (await readFile(config.tokenFile, "utf8")).trim(); } catch {}
    record("tokenFile", Boolean(token), "token_file_checked");
    let roots = Array.isArray(config.allowedRoots) && config.allowedRoots.length > 0;
    for (const root of config.allowedRoots || []) roots &&= await exists(root);
    record("workspace", roots, "workspace_checked");
    if (token) {
      try {
        const response = await fetch(`${config.apiBase}/health`, {
          headers: { Authorization: `Bearer ${token}` }, redirect: "error", signal: AbortSignal.timeout(5000),
        });
        const health = await response.json();
        record("api", response.ok && health.health_status === "ok", "api_health_checked");
      } catch { record("api", false, "api_unavailable"); }
    } else record("api", false, "token_required");
  } else {
    for (const name of ["inlineTokenAbsent", "tokenFile", "workspace", "api"]) record(name, false, "configuration_required");
  }
  const outbox = { pending: 0, held: 0 };
  if (config) {
    const root = path.join(stateDir, "outbox");
    for (const scope of await readdir(root).catch(() => [])) {
      for (const name of await readdir(path.join(root, scope)).catch(() => [])) {
        if (/^(turn|summary|distill)-.*\.json$/.test(name)) outbox.pending += 1;
        if (name.endsWith(".json.held")) outbox.held += 1;
      }
    }
  }
  record("noHeldJobs", outbox.held === 0, "outbox_checked");
  // A state directory that was renamed or replaced can strand held jobs that
  // no running adapter will ever look at again; surface them.
  outbox.legacyHeld = 0;
  const parent = path.dirname(stateDir);
  for (const name of await readdir(parent).catch(() => [])) {
    if (!name.startsWith(".aidumei-pi") || path.join(parent, name) === path.resolve(stateDir)) continue;
    const root = path.join(parent, name, "outbox");
    for (const scope of await readdir(root).catch(() => [])) {
      for (const job of await readdir(path.join(root, scope)).catch(() => [])) {
        if (job.endsWith(".json.held") || /^(turn|summary|distill)-.*\.json$/.test(job)) outbox.legacyHeld += 1;
      }
    }
  }
  record("noLegacyJobs", outbox.legacyHeld === 0, "legacy_state_checked");
  const ok = Object.values(checks).every(check => check.ok);
  console.log(JSON.stringify({ ok, action: "check", piVersion: pi.version, checks, outbox }));
  if (!ok) process.exitCode = 1;
}

async function main() {
  const opts = parse(process.argv.slice(2));
  if (opts.action === "--help") { console.log(help); return; }
  const piHome = path.resolve(opts["pi-home"] || path.join(os.homedir(), ".pi", "agent"));
  const stateDir = path.resolve(opts["state-dir"] || path.join(os.homedir(), ".aidumei-pi"));
  if (opts.action === "check") { await check(opts, piHome, stateDir); return; }
  if (opts.action === "uninstall") {
    if (!opts.yes) throw new Error("uninstall_requires_yes");
    const archive = await backup(piHome, stateDir);
    await rm(path.join(piHome, "extensions", "aidumei"), { recursive: true, force: true });
    await rm(path.join(stateDir, "config.json"), { force: true });
    console.log(JSON.stringify({ ok: true, action: "uninstall", backup: archive,
      preserved: "memory data, receipts and pending outbox jobs" }));
    return;
  }

  const testMode = Boolean(process.env.AIDUMEI_TEST_URL);
  const apiBase = (testMode ? process.env.AIDUMEI_TEST_URL : opts.api || "").replace(/\/+$/, "");
  const userId = opts.user || "";
  const bankId = opts.bank || "";
  const tokenFile = testMode ? process.env.AIDUMEI_TEST_TOKEN_FILE : opts["token-file"];
  if (!apiBase || !validScope(userId) || !validScope(bankId)) throw new Error("explicit_api_scope_required");
  if (!opts.workdir.length) throw new Error("workspace_required");
  if (!tokenFile || !path.isAbsolute(tokenFile)) throw new Error("absolute_token_file_required");
  if (!(await readFile(tokenFile, "utf8")).trim()) throw new Error("empty_token_file");
  const pi = checkPiBinary(opts["pi-bin"] || "pi");
  if (!pi.ok) throw new Error("unsupported_or_missing_pi");
  if (await readConfig(stateDir)) throw new Error("already_managed_uninstall_before_reconfigure");
  const allowedRoots = [];
  for (const root of opts.workdir) {
    if (!path.isAbsolute(root)) throw new Error("absolute_workspace_required");
    allowedRoots.push(await realpath(root));
  }
  const token = (await readFile(tokenFile, "utf8")).trim();
  const response = await fetch(`${apiBase}/health`, {
    headers: { Authorization: `Bearer ${token}` }, redirect: "error", signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("api_health_failed");
  const archive = await backup(piHome, stateDir);
  const target = path.join(piHome, "extensions", "aidumei");
  await mkdir(target, { recursive: true, mode: 0o700 });
  for (const file of ["index.ts", "package.json", "manage.mjs"]) {
    await atomic(path.join(target, file), await readFile(path.join(packageDir, file), "utf8"));
  }
  await mkdir(stateDir, { recursive: true, mode: 0o700 });
  await atomic(path.join(stateDir, "config.json"), JSON.stringify({
    apiBase, userId, bankId, tokenFile, allowedRoots, testMode, ...(opts.allowForget ? { allowForget: true } : {}),
  }, null, 2));
  console.log(JSON.stringify({ ok: true, action: "install", piVersion: pi.version, extensionTarget: target,
    configFile: path.join(stateDir, "config.json"), backup: archive,
    next: "Restart pi or run /reload; verify with node manage.mjs check." }));
}

main().catch(error => {
  console.error(`aidumei-pi: ${error?.message || "failed"}`);
  process.exitCode = 1;
});

export { backup, checkPiBinary, parse, readConfig };
