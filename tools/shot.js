#!/usr/bin/env node
/*
 * shot.js — scroll-aware panel screenshots via Chrome DevTools Protocol.
 *
 * Chrome's --screenshot flag captures the viewport only, and aiduMEI panels
 * are scroll containers, so the lower half of a panel never lands in a plain
 * headless capture. This drives a real Chrome over CDP instead: open the
 * panel, wait for its data, then capture each scroll position.
 *
 *   node shot.js <panel> [outPrefix]
 *   node shot.js pulse /tmp/mei_pulse
 */

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9333;
const URL_BASE = process.env.AIDUMEI_URL || 'http://127.0.0.1:8788/';

const panel = process.argv[2] || 'pulse';
const prefix = process.argv[3] || '/tmp/mei_' + panel;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function httpJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => {
        try { resolve(JSON.parse(d)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

async function main() {
  const chrome = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    '--remote-debugging-port=' + PORT,
    '--window-size=1440,940',
    '--force-device-scale-factor=2',
    'about:blank',
  ], { stdio: 'ignore' });

  // wait for the debugging endpoint to answer
  let tabs = null;
  for (let i = 0; i < 40; i++) {
    try { tabs = await httpJson('/json/list'); break; } catch (e) { await sleep(250); }
  }
  if (!tabs) { console.error('chrome did not come up'); chrome.kill(); process.exit(1); }

  const page = tabs.find((t) => t.type === 'page');
  const WebSocket = await loadWs();
  const ws = new WebSocket(page.webSocketDebuggerUrl);

  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg.result);
      pending.delete(msg.id);
    }
  });
  const send = (method, params) => new Promise((res) => {
    const myId = ++id;
    pending.set(myId, res);
    ws.send(JSON.stringify({ id: myId, method, params: params || {} }));
  });

  await new Promise((r) => ws.on('open', r));
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: URL_BASE + '#' + panel });
  await sleep(3500); // let the panel fetch and render

  // optional 4th arg: a query to type into the panel's search box, so panels
  // that only render on demand (忆思 / 追忆) can be captured too
  const query = process.argv[4];
  if (query) {
    await send('Runtime.evaluate', {
      expression: `(() => {
        const box = document.querySelector('#panelBody .sinput');
        const btn = document.querySelector('#panelBody .sbtn');
        if (!box || !btn) return 'no-search';
        box.value = ${JSON.stringify(query)};
        btn.click();
        return 'sent';
      })()`,
      returnByValue: true,
    });
    await sleep(4000); // the backend takes a few hundred ms plus rerank
  }

  const shots = [];
  for (let step = 0; step < 6; step++) {
    const { result } = await send('Runtime.evaluate', {
      expression: `(() => {
        const b = document.getElementById('panelBody');
        if (!b) return 'no-panel';
        const before = b.scrollTop;
        if (${step} > 0) b.scrollTop = before + b.clientHeight - 40;
        return JSON.stringify({ top: b.scrollTop, max: b.scrollHeight - b.clientHeight });
      })()`,
      returnByValue: true,
    });
    if (result.value === 'no-panel') { console.error('panel not open'); break; }
    const pos = JSON.parse(result.value);
    await sleep(450);
    const cap = await send('Page.captureScreenshot', { format: 'png' });
    const file = `${prefix}_${step}.png`;
    fs.writeFileSync(file, Buffer.from(cap.data, 'base64'));
    shots.push(file);
    console.log(`  ${file}  scrollTop=${Math.round(pos.top)} / ${Math.round(pos.max)}`);
    if (pos.top >= pos.max - 2) break;
  }

  ws.close();
  chrome.kill();
  console.log('shots:', shots.length);
}

/* ws is not in the stdlib; fall back to a tiny hand-rolled client if the
   module is unavailable so this script has no install step. */
async function loadWs() {
  try { return require('ws'); } catch (e) { /* fall through */ }
  return require(require('child_process')
    .execSync('npm root -g').toString().trim() + '/ws');
}

main().catch((e) => { console.error(e.message); process.exit(1); });
