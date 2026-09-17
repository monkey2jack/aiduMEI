# Host Agent Integration

aiduMEI is the durable memory layer. The host's native memory remains the short-term conversation layer. Do not copy the same memory into both systems: the host handles current turns and working context; aiduMEI stores facts, raw records, core memory, traces, and long-term evolution.

## The two wires (read this before anything else)

aiduMEI needs **two** independent hooks in your host. Wiring only one of them is
the single most expensive mistake you can make with this system, because the
failure is completely silent:

| Wire | When | What it does | If you skip it |
|---|---|---|---|
| **READ** | *before* the model answers | `/search` → inject relevant memories | The agent has amnesia — obvious immediately, you will fix it in minutes |
| **WRITE** | *after* the model answered | `/add` → persist what was just said | **Everything still looks fine.** Retrieval returns results, `/health` is green, dashboards are healthy — because the *old* memories really are healthy. Meanwhile every new thing you say is discarded. You notice weeks later, as a vague feeling that "it never remembers anything recent" |

We learned this the hard way on our own production deployment on 2026-09-17:
the read hook had been live for a month, the write hook had **never** been
wired. Every probe was green the whole time. It took a human auditing the
database by hand to find it.

So, before you trust this system with anything:

```bash
python3 scripts/check_ingest_wiring.py --token "$AIDUMEM_API_TOKEN"
```

It asks exactly one question — *you are reading; are you also writing?* —
and exits non-zero if the answer is no. Run it after deploying, and keep it
in your periodic checks. `/health` carries the same verdict as
`probes.ingest_liveness_ok`, and reports a `degraded` entry when a deployment
has been searching but not writing.

## Lifecycle

| Host moment | aiduMEI action | Wire |
|---|---|---|
| Before turn | Call `/gate`; if relevant, search or request context and inject once | READ |
| **After turn** | **Write user facts or durable decisions with `/add` — this is the WRITE wire; it is not optional** | **WRITE** |
| Before compression | Save at-risk raw dialogue with `/add/raw` | WRITE |
| Session end | Call `/session/end` to archive/report the session | — |
| Restore or migration | Import only durable facts and raw records, not transient working state | — |

### Where the write hook goes

Hook it to whatever your host calls "the turn just finished" — the moment the
assistant's reply is complete. Host-specific names differ; the shape does not:

| Host | Hook to use | Ready-made script in this repo |
|---|---|---|
| Hermes | `post_llm_call` (fires once per turn, after the tool loop) | `integrations/aidumem-ingest.sh` |
| Hermes (plugin route) | `MemoryProvider.sync_turn` — already wired | `integrations/hermes-plugin/aidumem/` |
| Claude Code | `Stop` hook | `integrations/cursor-hook/claude-code-stop-hook.py` |
| Anything else | The last callback in your turn pipeline, or a wrapper around your send-reply function | — |

### There is a third wire: session distill

Per-turn writes store *facts*. They cannot store **"what this whole stretch was
about"** — the offhand remark, the problem you solved together, the moment a
decision got made. Those end up scattered across a dozen extracted facts and
never surface again.

So a third hook, fired once when a session ends, distills the session into one
or two sentences and stores that as its own memory:

| Host | Hook | Ready-made script |
|---|---|---|
| Hermes | `on_session_end` | `integrations/aidumem-distill.sh` |
| Anything else | Whatever fires when a conversation closes | call `POST /session/distill`, then `POST /add` with what it returns |

Three properties worth knowing before you wire it:

- **It lives in its own decay lane** (`distill`, 0.3 — slower than ordinary
  memories). It deliberately does *not* reuse the `emotion` lane: that one decays
  at 150% because day-to-day mood swings *should* fade, and a session's takeaway
  fading faster than an ordinary fact would be absurd.
- **The "emotional weight" is not an invented score.** It counts hits against the
  emotion keyword list this repo already ships (`ducky/salience/config.py`), and
  that count nudges the initial salience within a bounded range. Every number
  traces back to a list you can read.
- **`/session/distill` only extracts; it does not store.** The hook does the
  second step (`POST /add`). That split is on purpose: the distilled line has to
  go through the full `/add` pipeline to reach the vector store — otherwise it
  cannot be recalled, which would defeat the whole point — and a pure extract
  endpoint is safe to re-run while debugging.

Missing this wire is quieter than missing the write wire: memories still arrive,
you just never get the "this stretch" layer. `/health` watches for it with
`distill_liveness_ok` — sessions arriving but zero distills means the hook is not
attached.

Do **not** put the write on the pre-turn hook. That hook runs *before* the
answer exists, so you would be recording half a conversation.

For Hermes, copying the script is not enough — it has to be **registered**:

```yaml
hooks:
  pre_llm_call:                                    # read wire
    - command: "~/.hermes/agent-hooks/aidumem-inject.sh"
      timeout: 8
  post_llm_call:                                   # write wire — the one people forget
    - command: "~/.hermes/agent-hooks/aidumem-ingest.sh"
      timeout: 10
  on_session_end:                                     # distill wire — "what this stretch was about"
    - command: "~/.hermes/agent-hooks/aidumem-distill.sh"
      timeout: 40
hooks_auto_accept: true
```

Then prove both ends work, in this order:

```bash
~/.hermes/agent-hooks/aidumem-inject.sh --selftest   # read wire
~/.hermes/agent-hooks/aidumem-ingest.sh --selftest   # write wire: writes one memory, reads it back
~/.hermes/agent-hooks/aidumem-distill.sh --selftest  # distill wire: endpoint present, verdict working
# ...have 5 real conversation turns, then:
python3 scripts/check_ingest_wiring.py               # non-zero exit = still not wired
```

The third command is the only one that proves the *host* is calling the script.
The first two only prove the script itself runs — in the incident that produced
this section, the scripts were fine the whole time; nobody had hooked the write one.

In CI or a release gate, add `--require-judgment`. Without it the check returns 0
when there is not enough traffic to judge — deliberately, so a brand-new install
is not blocked by a false red. But that also means a freshly installed *broken*
system sails straight through. `--require-judgment` turns "cannot tell" into a
non-zero exit.

### The read wire must pass `session_id` too

`aidumem-inject.sh` sends `session_id` on its `/search` call, and a custom read
wire should do the same. Two things depend on it:

- **Echo suppression (M2)** — the server excludes memories written by the current
  session. With no session it cannot tell which those are, so it excludes nothing.
  The feature does not error; it just quietly does nothing.
- **The write-wire probe** — retrieval logs distinguish "someone is actually
  talking" from "the hourly smoke test ran" purely by whether a session is
  attached. A read wire that omits it leaves `ingest_liveness` with no reach at
  all, and `/health` will say so rather than pretend everything is fine.

### Always pass `session_id` and `turn`

Include them in the write payload's `metadata`:

```json
{
  "messages": [{"role": "user", "content": "..."},
               {"role": "assistant", "content": "..."}],
  "user_id": "alice", "bank_id": "default", "infer": true,
  "metadata": {
    "_origin_session_id": "<your host's session id>",
    "_origin_agent": "<your agent name>",
    "_origin_turn": 7
  }
}
```

Memories are **not** scoped to a session — retrieval always searches the whole
bank, so starting a new session (`/new`, `/clear`, a fresh process) never loses
anything. `session_id` is used for two other things:

- **echo suppression** — a memory written in this very session is not fed back
  to you as "something you remembered" two turns later;
- **trajectory credit** — feedback on a task is distributed across the steps
  that led to it.

Omit it and both features sit there doing nothing, silently. `/health` reports
the coverage as `probes.epistemic_session_coverage`; a deployment that writes
memories but never attaches a session id will see that stay at `0`.

### Belt and braces: a periodic sweep

A hook can be misconfigured, disabled during an upgrade, or silently throw.
For anything you actually care about remembering, add a scheduled job as a
second line of defence — it re-reads recent conversations from your host's own
store and writes anything the hook missed, **and it alerts when it finds a gap**.
Catching the gap is the point; back-filling is the bonus.

## Scope model

- `user_id`: identity namespace for the human/operator.
- `bank_id`: semantic workspace (for example personal, work, or project).
- Session: conversation-scoped reporting and lifecycle; it is not a tenant replacement.

Write and search must use the same `(user_id, bank_id)`. If a write omitted `bank_id`, it belongs to `default`.

## Avoid double injection

1. Choose one injection source per turn.
2. If the host already injects native memory, use aiduMEI for durable facts only.
3. Keep native working memory out of aiduMEI except when explicitly archiving raw conversation.
4. Verify with `scripts/e2e_smoke.py`: one nonce write must yield one found recall, not repeated context blocks.

## Migrating old memory

1. Export durable facts, preferences, decisions, and raw source records.
2. Import via `/add` or `/add/raw`; include source metadata.
3. Do not import transient state, tool logs, or temporary scratch notes.
4. Run searches from a new session and inspect `/search_trace`.
5. Keep the original export until e2e smoke passes and a backup is verified.

## Non-Hermes hosts

Use the HTTP API as the single integration surface: `/gate`, `/search`, `/add`, `/add/raw`, `/session/start`, and `/session/end`. The Hermes plugin is a convenience wrapper around the same lifecycle, not a required host.

## MCP server (port 8766)

The built-in MCP server exposes the same operations over stdio or SSE. Its write tools land on the REST API and share the REST credential. Trust model: stdio and loopback-bound SSE (`127.0.0.1`/`localhost`/`::1`) trust the local machine; **SSE bound to a non-loopback address refuses to start unless `AIDUMEM_API_TOKEN` is configured** (sent as Bearer), so the MCP surface cannot bypass REST auth. The explicit escape hatch `AIDUMEM_ALLOW_INSECURE_PUBLIC=1` lifts the refusal (off by default, critical-level log when on).
