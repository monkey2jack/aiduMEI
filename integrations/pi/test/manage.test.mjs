import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, readFile, readdir, rm, stat, writeFile, chmod } from "node:fs/promises";
import { createServer } from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { gunzipSync } from "node:zlib";

const run = promisify(execFile);
const manage = fileURLToPath(new URL("../manage.mjs", import.meta.url));
const packageFile = fileURLToPath(new URL("../package.json", import.meta.url));

async function exists(file) { try { await stat(file); return true; } catch { return false; } }

test("package declares a native Pi extension with no runtime dependencies", async () => {
  const manifest = JSON.parse(await readFile(packageFile, "utf8"));
  assert.deepEqual(manifest.pi.extensions, ["./index.ts"]);
  assert.equal(manifest.dependencies, undefined);
  assert.equal(manifest.engines.node, ">=22.19.0");
});

test("install, check and uninstall stay inside temporary Pi and state homes", async () => {
  const root = path.join(os.tmpdir(), `aidumei-pi-manage-${process.pid}-${Date.now()}`);
  await mkdir(root, { recursive: true });
  const piHome = path.join(root, "pi-home");
  const stateDir = path.join(root, "state");
  const workdir = path.join(root, "workspace");
  const piBin = path.join(root, process.platform === "win32" ? "pi-bin.cmd" : "pi-bin");
  const tokenFile = path.join(root, "token");
  await mkdir(workdir, { recursive: true });
  await writeFile(piBin, process.platform === "win32" ? "@echo off\r\necho 1.6.0\r\n" : "#!/bin/sh\necho 1.6.0\n");
  if (process.platform !== "win32") await chmod(piBin, 0o700);
  await writeFile(tokenFile, "test-token", { mode: 0o600 });
  const server = createServer((_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end('{"status":"ok","health_status":"ok"}');
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const env = { ...process.env, AIDUMEI_TEST_URL: `http://127.0.0.1:${server.address().port}`,
    AIDUMEI_TEST_TOKEN_FILE: tokenFile };
  const common = ["--pi-home", piHome, "--pi-bin", piBin, "--state-dir", stateDir];
  try {
    const installed = await run(process.execPath, [manage, "install", ...common,
      "--user", "dudu", "--bank", "default", "--workdir", workdir], { env });
    const installReport = JSON.parse(installed.stdout);
    assert.equal(installReport.ok, true);
    assert.equal(await exists(path.join(piHome, "extensions", "aidumei", "index.ts")), true);
    const config = JSON.parse(await readFile(path.join(stateDir, "config.json"), "utf8"));
    assert.equal(config.apiToken, undefined);
    assert.equal(config.token, undefined);
    const checked = await run(process.execPath, [manage, "check", ...common], { env });
    const checkReport = JSON.parse(checked.stdout);
    assert.equal(checkReport.ok, true);
    assert.equal(checkReport.piVersion, "1.6.0");
    const removed = await run(process.execPath, [manage, "uninstall", ...common, "--yes"], { env });
    const uninstallReport = JSON.parse(removed.stdout);
    assert.equal(uninstallReport.ok, true);
    assert.equal(typeof uninstallReport.backup, "string");
    const archive = gunzipSync(await readFile(uninstallReport.backup));
    assert.match(archive.toString("latin1"), /config\.json/);
    assert.match(archive.toString("latin1"), /extension\/index\.ts/);
    assert.equal(await exists(path.join(piHome, "extensions", "aidumei")), false);
    assert.equal(await exists(path.join(stateDir, "config.json")), false);
    assert.equal(await exists(path.join(stateDir, "outbox")), false);
  } finally {
    server.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("production install refuses missing scope and token without writing files", async () => {
  const root = path.join(os.tmpdir(), `aidumei-pi-refuse-${process.pid}-${Date.now()}`);
  await mkdir(root, { recursive: true });
  const piHome = path.join(root, "pi-home");
  const stateDir = path.join(root, "state");
  try {
    await assert.rejects(run(process.execPath, [manage, "install", "--pi-home", piHome, "--state-dir", stateDir], {
      env: { ...process.env, AIDUMEI_TEST_URL: "", AIDUMEI_TEST_TOKEN_FILE: "" },
    }));
    assert.equal(await exists(piHome), false);
    assert.equal(await exists(stateDir), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("check reports jobs stranded in a replaced state directory", async () => {
  const root = path.join(os.tmpdir(), `aidumei-pi-legacy-${process.pid}-${Date.now()}`);
  const stateDir = path.join(root, ".aidumei-pi-current");
  const legacy = path.join(root, ".aidumei-pi", "outbox", "scope");
  try {
    await mkdir(stateDir, { recursive: true });
    await mkdir(legacy, { recursive: true });
    await writeFile(path.join(legacy, "distill-abc.json.held"), "{}");
    let output = "";
    try {
      const result = await run(process.execPath, [manage, "check", "--pi-home", path.join(root, "pi-home"),
        "--state-dir", stateDir, "--pi-bin", path.join(root, "missing-pi")], { env: { ...process.env } });
      output = result.stdout;
    } catch (error) { output = error.stdout; }
    const report = JSON.parse(output);
    assert.equal(report.outbox.legacyHeld, 1);
    assert.equal(report.checks.noLegacyJobs.ok, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
