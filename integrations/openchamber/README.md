# aiduMEI OpenChamber adapter

Memory bridge for OpenChamber **2.7.2** and its OpenCode **2.3.0** server. This is
the fifth host after Claude Code, Codex, Hermes and DSH. Other versions require a
new compatibility review; the plugin rejects an explicitly different OpenCode
version. Runtime dependencies are Node built-ins only.

## Install and permitted workspaces

```bash
node integrations/openchamber/manage.mjs install \
  --api http://127.0.0.1:8767 --user example --bank example \
  --token-file /path/to/identity-bound-token \
  --allow-root /path/to/permitted/workspace
```

Repeat `--allow-root` for additional roots. The default list is empty: recall,
prompt recording and ingestion are disabled. Both the plugin and the service
independently resolve real paths before checking containment. Symlinks, Windows
junctions and short names do not bypass the check. Unknown directories fail
closed; child sessions are excluded by the service.

Restart the pinned host and approve the extension in Settings > Extensions.
`manage.mjs` copies both `plugin/index.js` and its built-in-only `runtime.mjs`.
The extension service runs `service/main.js` from the complete package.

## Data flow and reliability

At prompt admission, the plugin records an immutable prompt with the host's
`messageID`, a timestamp and a unique file name. It completes a bounded 3-second
recall prefetch before returning. The context hook injects an already resolved
value synchronously, guards each message ID separately and wraps memory in a
nonce data fence in the system array. Tool continuations do not inject again.
Searches and writes use the same `och-<sessionID>` memory session identity.

OpenCode 2.3.0's actual Promise adapter **awaits** prompt and context callbacks:
[`promise/adapter.ts`](https://github.com/anomalyco/opencode/blob/v2.3.0/packages/plugin/src/promise/adapter.ts),
[`hooks.ts`](https://github.com/anomalyco/opencode/blob/v2.3.0/packages/core/src/plugin/hooks.ts),
[`model-request.ts`](https://github.com/anomalyco/opencode/blob/v2.3.0/packages/core/src/session/model-request.ts).
The former blanket claim that this version does not await context was incorrect.
The deterministic test additionally copies the final model request without
awaiting the context callback, after awaited prompt admission, and asserts a
nonempty data fence. This test is not a substitute for installed-host acceptance.

OpenChamber 2.7.2's completion event has no turn/message ID. The service uses a
bounded FIFO prompt queue within the completion timestamp rather than overwriting
one file per session. This preserves the order of ordinary consecutive prompts;
interrupted or steered turns still require installed-host acceptance. A prompt
without a matching completion is not silently paired outside its 24-hour window.
Active plugin instances expire old prompts every ten minutes and on startup.

The service commits the complete question/answer payload to an atomic, fsynced
outbox before consuming its prompt. Replaying the same event never consumes the
next prompt. Stable idempotency keys protect `/add` retries. Permanent rejection
keeps the original payload as `.held`; temporary failures retry after 30 seconds.
The host receives a persistence failure when a durable commit fails.

Idle distill plans are persisted before event acknowledgement and restored after
a restart. A plan waits for that session's pending/held turns, then calls
`/session/distill`, persists the returned summary and writes it through `/add`
with the unchanged server seal. An old plan cannot delete a newer plan revision.

## Credentials and test isolation

Production config at `~/.aidumei-openchamber/config.json` contains only the token
file path, endpoint, identity and `allowedRoots`. Both processes re-read the token
file for each request. Legacy inline-token configs require reinstalling and are
rejected by the service and `check`.

`AIDUMEI_TEST_URL` selects a separate `~/.aidumei-openchamber/test/` realm for the
plugin: config, prompts, outbox, receipts and distill plans are all separate. It
uses only test credentials. A production service never consumes that realm.
To run the test service, pass `--test` or set `AIDUMEI_TEST_URL` in its own
environment. OpenChamber scrubs service environment variables, so a test extension
must use a separate host HOME or explicitly launch the test service; setting a
variable only on the plugin does not convert the production service into a test
service. `AIDUMEI_STATE_DIR` selects an alternative root only when set consistently
on both processes; `manage.mjs --state-dir` uses the same root.

No log contains prompts, replies, recalled memory or system text. State contains
conversation data and must remain private to the OS user. `AIDUMEI_HOOKS=off`
disables both processes.

## Check and uninstall

```bash
node integrations/openchamber/manage.mjs check
node integrations/openchamber/manage.mjs uninstall --yes
```

Check reports plugin/config/API status, state realm, allowed-root count,
pending/held jobs and actionable fixes. Empty roots are a safe disabled state.
Held jobs fail the check. Identity-bound keys are required for installation;
current check does not certify server-side key binding.

Uninstall removes the installed plugin and config. It retains outbox/receipts for
operator review and does not change memories already stored by the API.

## Verification

```bash
node --test integrations/openchamber/test/*.test.mjs
```

Tests use temporary HOME directories and fake local APIs. They cover actual
request construction, deny-by-default workspaces, symlink escape, token rotation,
test state separation, duplicate prompts/events, failed persistence, held jobs,
sealed summary writeback and restart recovery. Installed OpenChamber/OpenCode,
Windows native filesystem behavior and CC production acceptance remain separate
release gates.
