# DeepSeek Harness preview

This f0.8++ candidate adds automatic recall, committed-turn ingestion, idle
session distillation and the existing aiduMEI MCP tools to **DSH 0.2.2-rc.3**.
The adapter lives in this directory. Claude Code and Codex keep their existing
hooks. The aiduMEI API continues to run on its server; Windows runs the DSH
client and, optionally, the Python stdio MCP launcher.

Use Node **24.9 or newer** (validated host: 24.21.0), the fixed DSH version and
an aiduMEI checkout. A future DSH release requires compatibility review. Do not
use a version exemption to skip it. MCP additionally needs Python 3.10 or newer
and `pip install -r requirements.txt` in a separate virtual environment. The
launcher uses the checkout's `mcp_server.py`; a standalone npm package alone
cannot supply the Python server modules.

## Windows installation

Fully quit the DSH profile being changed. Start with a separate preview profile.
The commands below create a profile from the shipped headless template; they
do not copy the active desktop profile's credentials or change its settings.
Run from the aiduMEI checkout in PowerShell:

```powershell
$deps = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies'
$node = Join-Path $deps 'node\bin\node.exe'
$cli = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'
$previewHome = Join-Path $env:LOCALAPPDATA 'aiduMEI\dsh-preview-home'
$tokenFile = Join-Path $env:USERPROFILE '.aidumei-keys\dsh-preview.key'
# Provision a scoped token file through your existing secret management.
# The file should be readable only by the account running DSH.
& $node integrations/dsh/manage.mjs install --dsh-cli $cli --dsh-home $previewHome --profile aidumei-preview --api https://memory.example.invalid --user dsh-preview --bank dsh-preview --workdir 'C:\Work\Project' --token-file $tokenFile
& $node integrations/dsh/manage.mjs --check --dsh-cli $cli --dsh-home $previewHome --profile aidumei-preview
$env:DSH_HOME = $previewHome
& $cli --profile aidumei-preview
```

Replace the example API, workspace and token-file paths with your own. Keep the
checkout in place: DSH installs this local package by reference. To enable MCP,
add `--python 'C:\path\to\venv\Scripts\python.exe'` to the install command.
The installer uses the Desktop carrier directly so paths containing spaces do
not pass through a command shell. It never starts a Windows aiduMEI API service.

Linux and macOS use the same manager with `node`, `--dsh-cli /absolute/path/to/dsh`
and native absolute paths. For the npm host, `DSH_CLI_ENTRY` can name the
installed `@deepseek-ai/dsh/lib/bin.js`. Put the host's Node and pnpm on `PATH`.

The manager backs up `package.json`, `cordis.patch.yml`, `pnpm-lock.yaml` and
`pnpm-workspace.yaml` under the selected profile's `.aidumei-backups/` before
installing. Existing YAML block-sequence patches are preserved. Convert a
nonempty JSON/flow-style patch to a YAML block sequence before installation;
the manager refuses to rewrite an unknown configuration format.

`--check` verifies the fixed DSH version, managed configuration, bundle selection,
token file, workspace existence and authenticated API health. It does not
replace the actual session/MCP smoke test. Check Python dependencies separately:

```text
python integrations/dsh/mcp-stdio.py --check
```

## Isolation and behavior

`allowedRoots` must explicitly name existing absolute workspaces. The shipped
bundle's empty allowlist disables automatic traffic. Reads, writes and replay
check the real workspace path, including Windows junctions. Subagents and
fork-inherited turns are excluded by default. Use a distinct profile/`instanceId`
for simultaneously running host processes so each owns its own spool.

The first eligible step of a human turn fetches CoreMemory and search results
in parallel with a three-second timeout. Recall is bounded and wrapped in a
fresh nonce data fence. Failed recall lets the conversation proceed; three
failed requests open a one-minute circuit. Synthetic recall, skills and tool
messages are excluded from ingestion. On `turn/end` the bridge saves real user
text and the last nonempty assistant text, including cancelled turns that have
committed text. Text blocks are joined and outer whitespace is trimmed.

The default `infer: false` preserves raw turn text without running an LLM for
each write. Distillation is scheduled after two idle minutes or session disposal;
it depends on the server's configured summarizer/fallback and may report a
legitimate skipped result. Host flushing waits for local writes, not network
delivery. Exit can therefore leave durable pending jobs for the next launch.

Set `AIDUMEI_HOOKS=off` (also `0` or `false`) before starting DSH to disable
automatic hooks, recovery and MCP launch. No spool is created while disabled.
An explicit test endpoint uses **only** `AIDUMEI_TEST_TOKEN_FILE` or
`AIDUMEI_TEST_TOKEN`; it never falls back to a production token. Test mode is
also part of the queue's scope. The manager requires a token file:

```text
AIDUMEI_TEST_URL=http://127.0.0.1:8878
AIDUMEI_TEST_TOKEN_FILE=/absolute/path/to/test.token
```

MCP uses stdio because this DSH host does not accept aiduMEI's SSE transport.
Its launcher disables repository `.env` loading, host chat-database scanning
and dangerous MCP tools. The configured user and bank are the scoped MCP
defaults; tool calls may provide explicit values when intentionally accessing
another scope. The optional server default-bank environment variable is set
only in the DSH MCP process; every other host keeps its historical default.
The installer stores endpoint, identity and the token **path**, never token
contents. Keep profile backups and logs private.

## Recovery and uninstall

The spool is under `%LOCALAPPDATA%\aiduMEI\dsh\spool` on Windows and
`${XDG_STATE_HOME:-~/.local/state}/aiduMEI/dsh/spool` on Unix. Endpoint, user,
bank, test mode and instance identify its subdirectory. Pending payloads contain
conversation text; restrict directory access using the operating system's ACLs.
POSIX directories/files use 0700/0600. `chmod` is not a Windows ACL guarantee.

- Network errors, 408, 429 and server errors retry. Stable idempotency keys and
  durable receipts suppress acknowledged turn replays after host restart.
- Permanent 4xx responses, repair-required 409 and invalid acknowledgements
  retain the exact job as `.held`. Inspect and repair the cause before manually
  retrying. A held turn blocks its session's summary; do not delete it to hide
  the error.
- If `.owner/pid.json` names a live process, the second adapter instance disables
  itself. A dead PID is reclaimed. An unreadable owner or reused PID requires
  review: fully stop this profile, archive the spool, verify no owner is alive,
  then remove only the stale `.owner` directory.
- Queue capacity is 256 jobs. Full or unwritable queues emit
  `local_persistence_failed`; reopening a session rescans its durable DSH log.
  Recovery of a session that never reopens is outside this preview's guarantee.
- Receipts are retained without automatic pruning in this preview. Atomic
  renames protect against partial JSON files, but no filesystem `fsync` guarantee
  is made for power loss. Keep ordinary host and server backups.

Uninstall with the same CLI, home and profile, after fully quitting its processes:

```text
node integrations/dsh/manage.mjs uninstall --dsh-home /path/to/preview-home --profile aidumei-preview
```

The manager removes its patch and package, preserves unrelated patch edits when
the managed block is intact, and retains backups plus all pending jobs. If the
managed block itself changed, uninstall refuses automatic edits; compare the
saved backup first. A partially failed install remains marked `installing` so
uninstall can recover it. Credentials and memory data are never deleted.

## Verification

```text
node --test integrations/dsh/test/*.test.mjs
python -m pytest tests/test_f08pp_dsh_launcher.py -q
node integrations/dsh/test/host-smoke.mjs
```

For the last command set `DSH_CLI_ENTRY` (npm host) or `DSH_CLI_CMD` (Desktop
`dsh.cmd`) and optionally `DSH_MCP_PYTHON`. It creates a temporary DSH home,
installs/checks/uninstalls the bundle, uses actual DSH sessions and a deterministic
local model fixture, and asserts the MCP result contains a seeded memory.
`DSH_SMOKE_SESSIONS=5`, `DSH_SMOKE_TURNS=3`, `DSH_SMOKE_FAULTS=1` additionally
exercise a temporary outage, cancellation, killed process and resumed sessions.
`AIDUMEI_TEST_URL` selects an isolated real API behind the test proxy. Reports
identify fixture models explicitly; a passing fixture test is not evidence of
production model quality or completed Windows/production acceptance.
