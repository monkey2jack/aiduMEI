# aiduMEI OpenChamber adapter

Memory bridge for [OpenChamber](https://github.com/openchamber/openchamber) (OpenCode
workspace, v2.6.1+). Fourth first-class host after Claude Code, Codex, Hermes and DSH.

## What it does

| Line | Mechanism | Notes |
|---|---|---|
| **Recall injection** | OpenCode plugin (`contributes.opencodePlugin`): `ctx.session.hook("context")` | First model call of a turn: injects a nonce-fenced recall block into the messages. Turn guard prevents double-injection in the agent loop. 3 s timeout, 3-fail circuit breaker. |
| **Turn ingestion** | `contributes.service` + `contributes.sessionEvents` (turn-finished / turn-failed, `content: true`) | OpenChamber POSTs events to the local service; the service pairs the reply with the user prompt (recorded by the plugin), spools durably, and forwards to aiduMEI `/add` with an idempotency key. |
| **Idle distillation** | service timer | 2 minutes after the last event → `POST /session/distill`. Best-effort. |

Zero runtime dependencies: the plugin runs inside OpenCode (bare `import()`, no
`node_modules`), the service is spawned by OpenChamber with a scrubbed
environment (only PATH/HOME/temp/locale pass through).

## Install

```bash
node integrations/openchamber/manage.mjs install \
  --api http://127.0.0.1:8767 \
  --user <your-id> --bank <your-bank> \
  --token-file /path/to/token
```

Then restart OpenCode/OpenChamber. The plugin appears in **Settings → Plugins**;
approve the extension in **Settings → Extensions** (service + sessionEvents lines).

## Check

```bash
node integrations/openchamber/manage.mjs check
```

Verifies plugin presence, config validity, and aiduMEI API health.

## Uninstall

```bash
node integrations/openchamber/manage.mjs uninstall --yes
```

Removes the plugin and shared config. Memory data in the aiduMEI API is
**untouched**; the spool is left in place for manual review.

## Configuration

All configuration lives in `~/.aidumei-openchamber/config.json` (written by
`manage.mjs install`, read by both the plugin and the service). The service
environment does **not** inherit the host's variables — this file is the only
channel.

| Key | Meaning |
|---|---|
| `apiBase` | aiduMEI API base URL |
| `apiTokenFile` | token file path (preferred; token content never in this file) |
| `apiToken` | inline token (fallback) |
| `userId` / `bankId` | memory scope |
| `testMode` | when true, only the test token is used |

## Isolation and safety

- Default allowlist is empty; the adapter does not write to any project unless
  explicitly configured.
- Test mode (`--test-url` / `--test-token`) is fully isolated from production.
- `AIDUMEI_HOOKS=off` disables everything at plugin and service level.
- The recall fence (`[DATA:nonce] … [END OF DATA CONTEXT]`) is the same format
  as the DSH adapter: data, never instructions.

## Version anchor

Validated against OpenChamber v2.7.2 + OpenCode v2.3.0 (the versions shipped
together in the OpenChamber desktop installer). Newer OpenChamber minor
versions are expected to work (the SDK contract is `apiVersion: 1`); older
ones may lack the `sessionEvents` contribution.
